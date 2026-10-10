"""Disc images to CHD with chdman.

A "game" is one per-game archive (.zip/.7z), one sub-folder holding a disc
set, or one loose descriptor in the input folder. Each disc descriptor
(.gdi > .cue > .toc > .iso) becomes <name>.chd in /roms/<system>/.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import archives, fsutil
from .config import Config
from .systems import System

DESCRIPTOR_PRIORITY = (".gdi", ".cue", ".toc", ".iso")
UNSUPPORTED = (".ccd", ".img", ".sub", ".mds", ".mdf", ".nrg", ".cdi")
DISC_EXTS = set(DESCRIPTOR_PRIORITY) | set(UNSUPPORTED) | {".bin", ".raw", ".wav"}


@dataclass
class Game:
    name: str
    kind: str                 # archive | folder | loose
    path: Path
    status: str = "convert"   # convert | present | unsupported | empty
    detail: str = ""
    targets: list[str] = field(default_factory=list)
    size: int = 0


def pick_descriptors(names: list[str]) -> tuple[list[str], str]:
    """Choose descriptor files from a list of paths. Returns (descriptors, problem)."""
    by_ext: dict[str, list[str]] = {}
    for n in names:
        by_ext.setdefault(Path(n).suffix.lower(), []).append(n)
    for ext in DESCRIPTOR_PRIORITY:
        if ext in by_ext:
            return sorted(by_ext[ext], key=str.casefold), ""
    odd = sorted({Path(n).suffix.lower() for n in names if Path(n).suffix.lower() in UNSUPPORTED})
    if odd:
        return [], f"unsupported layout ({', '.join(odd)}); convert to .cue/.bin first"
    return [], "no disc descriptor (.cue/.gdi/.toc/.iso) found"


def _targets(descriptors: list[str], game_name: str) -> list[str]:
    if len(descriptors) == 1:
        return [game_name]
    return [Path(d).stem for d in descriptors]  # multi-disc set: one CHD per descriptor


def discover(cfg: Config, sysdef: System, input_path: Path) -> list[Game]:
    """List games under an input folder (or a single archive) and decide each one."""
    input_path = Path(input_path)
    folder = cfg.system_dir(sysdef.key)
    have = {e.name.casefold() for e in os.scandir(folder)} if folder.is_dir() else set()
    games: list[Game] = []
    if input_path.is_file():
        candidates = [input_path]
    else:
        candidates = sorted(input_path.iterdir(), key=lambda p: p.name.casefold())

    loose_names: list[str] = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        if p.is_file() and p.suffix.lower() in archives.ARCHIVE_EXTS:
            g = Game(p.stem, "archive", p, size=p.stat().st_size)
            try:
                names = [e.name for e in archives.list_entries(p) if not e.is_dir]
            except Exception as e:  # noqa: BLE001
                g.status, g.detail = "unsupported", f"cannot read archive: {e}"
                games.append(g)
                continue
            descs, problem = pick_descriptors(names)
            g.targets = _targets(descs, g.name)
            if problem:
                g.status, g.detail = ("unsupported" if "unsupported" in problem else "empty"), problem
            games.append(g)
        elif p.is_dir():
            names = [str(Path(r) / f) for r, _d, fs in os.walk(p) for f in fs]
            g = Game(p.name, "folder", p, size=sum(Path(n).stat().st_size for n in names))
            descs, problem = pick_descriptors(names)
            g.targets = _targets(descs, g.name)
            if problem:
                g.status, g.detail = ("unsupported" if "unsupported" in problem else "empty"), problem
            games.append(g)
        elif p.is_file():
            loose_names.append(str(p))

    # Loose sets directly in the input folder: one game per descriptor.
    descs, problem = pick_descriptors(loose_names)
    for d in descs:
        games.append(Game(Path(d).stem, "loose", Path(d), targets=[Path(d).stem], size=Path(d).stat().st_size))
    if not descs and problem.startswith("unsupported"):
        for n in loose_names:
            if Path(n).suffix.lower() in (".ccd", ".mds", ".nrg", ".cdi"):
                games.append(Game(Path(n).stem, "loose", Path(n), "unsupported", problem))

    for g in games:
        if g.status == "convert" and g.targets and all(f"{t}.chd".casefold() in have for t in g.targets):
            g.status, g.detail = "present", "CHD already exists"
    return games


# ---- cue parsing -----------------------------------------------------------------

_FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.I)


def cue_files(cue: Path) -> list[str]:
    files = []
    for line in cue.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _FILE_RE.match(line)
        if m:
            files.append(m.group(1) or m.group(2))
    return files


def gdi_files(gdi: Path) -> list[str]:
    out = []
    lines = gdi.read_text(encoding="utf-8", errors="replace").splitlines()[1:]
    for line in lines:
        parts = shlex.split(line) if line.strip() else []
        if len(parts) >= 5:
            out.append(parts[4])
    return out


def resolve_tracks(desc: Path) -> tuple[list[Path], list[str]]:
    """Return (present track files, missing names) referenced by a descriptor."""
    ext = desc.suffix.lower()
    if ext == ".cue":
        refs = cue_files(desc)
    elif ext == ".gdi":
        refs = gdi_files(desc)
    else:
        return [], []
    present, missing = [], []
    siblings = {p.name.casefold(): p for p in desc.parent.iterdir()}
    for r in refs:
        p = desc.parent / r
        if p.exists():
            present.append(p)
        elif Path(r).name.casefold() in siblings:
            present.append(siblings[Path(r).name.casefold()])
        else:
            missing.append(r)
    return present, missing


# ---- chdman ----------------------------------------------------------------------

def chdman_command(cfg: Config, sysdef: System, desc: Path, out: Path) -> list[str]:
    mode = "createdvd" if (sysdef.disc == "dvd" and desc.suffix.lower() == ".iso") else "createcd"
    return shlex.split(cfg.tools.chdman) + [mode, "-i", str(desc), "-o", str(out)]


def run_chdman(ctx, cmd: list[str]) -> tuple[int, str]:
    """Run chdman, logging progress lines; kill it if the job is cancelled."""
    ctx.log("$ " + " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    tail: list[str] = []
    buf = b""
    last_progress = 0.0
    try:
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail = (tail + [line])[-40:]
                if "complete" in line.lower() and "%" in line:
                    now = time.monotonic()
                    if now - last_progress < 5:
                        continue  # chdman prints progress many times a second
                    last_progress = now
                ctx.log("  " + line)
            ctx.check()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if buf.strip():
        tail.append(buf.decode("utf-8", "replace").strip())
    return proc.wait(), "\n".join(tail)


def _is_cancel(e: BaseException) -> bool:
    return type(e).__name__ == "Cancelled"


def convert_game(ctx, cfg: Config, sysdef: System, g: Game, run=run_chdman, backup: bool = True) -> None:
    folder = fsutil.ensure_dir(cfg.system_dir(sysdef.key))
    work = cfg.temp_dir / f"chd-{ctx.id}" / re.sub(r"[^\w.() -]", "_", g.name)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    keep_temp = False
    try:
        if g.kind == "archive":
            ctx.log(f"Extracting {g.path.name} to {work}")
            archives.extract_all(g.path, work / "src")
            src_root = work / "src"
        elif g.kind == "folder":
            src_root = g.path
        else:
            src_root = g.path.parent
        if g.kind == "loose":
            descs = [g.path]
        else:
            names = [str(Path(r) / f) for r, _d, fs in os.walk(src_root) for f in fs]
            found, problem = pick_descriptors(names)
            if problem:
                ctx.count("unsupported")
                ctx.item(g.name, "unsupported", problem)
                ctx.log(f"UNSUPPORTED {g.name}: {problem}")
                return
            descs = [Path(d) for d in found]
        targets = _targets([str(d) for d in descs], g.name)
        ok_all = True
        before_total = after_total = 0
        for desc, target in zip(descs, targets):
            ctx.check()
            dest = folder / f"{target}.chd"
            if dest.exists():
                ctx.count("present")
                ctx.item(f"{target}.chd", "present", "CHD already exists")
                continue
            tracks, missing = resolve_tracks(desc)
            if missing:
                ok_all = False
                keep_temp = g.kind == "archive"
                ctx.count("failed")
                ctx.item(f"{target}.chd", "failed", f"{desc.name} references missing track file(s): {', '.join(missing)}")
                ctx.log(f"FAIL {target}: missing tracks {missing}")
                continue
            before = desc.stat().st_size + sum(t.stat().st_size for t in tracks)
            if desc.suffix.lower() == ".iso":
                before = desc.stat().st_size
            out = work / f"{target}.chd"
            code, output = run(ctx, chdman_command(cfg, sysdef, desc, out))
            if code != 0 or not out.exists():
                ok_all = False
                keep_temp = True
                ctx.count("failed")
                ctx.item(f"{target}.chd", "failed", f"chdman exited with {code}; temp files kept in {work}",
                         stderr=output[-4000:])
                ctx.log(f"FAIL {target}: chdman exit {code}")
                continue
            after = out.stat().st_size
            ctx.log(f"Copying {out.name} to {folder}")
            fsutil.copy_new(out, dest, ctx.check)
            if dest.stat().st_size != after:
                raise IOError(f"size mismatch after copying {out.name}")
            out.unlink()
            before_total += before
            after_total += after
            ctx.count("converted")
            ctx.count("bytes_before", before)
            ctx.count("bytes_after", after)
            ratio = after / before if before else 0
            ctx.item(f"{target}.chd", "added", f"{len(tracks) or 1} track file(s)", before=before, after=after,
                     ratio=ratio)
            ctx.log(f"OK   {target}.chd  {before:,} -> {after:,} bytes ({ratio:.1%})")
        if ok_all and before_total and backup:
            _backup_original(ctx, cfg, sysdef, g)
    except BaseException as e:
        if not _is_cancel(e):
            keep_temp = True
        raise
    finally:
        if not keep_temp:
            shutil.rmtree(work, ignore_errors=True)
        else:
            ctx.log(f"Temp files kept for inspection: {work}")


def _backup_original(ctx, cfg: Config, sysdef: System, g: Game) -> None:
    if g.kind != "archive":
        ctx.log(f"{g.name}: loose/folder originals left in place ({g.path})")
        return
    backup_dir = cfg.source_dir / sysdef.key
    if fsutil.is_within(g.path, cfg.source_dir):
        ctx.log(f"{g.path.name} is already inside _source; left as is")
        return
    dest = backup_dir / g.path.name
    if dest.exists():
        ctx.log(f"_source/{sysdef.key}/{g.path.name} already exists; original left in place")
        ctx.item(g.path.name, "skipped", "backup already exists; original not moved")
        return
    ctx.log(f"Backing up {g.path.name} -> _source/{sysdef.key}/")
    fsutil.safe_move(g.path, dest, ctx.check)
    ctx.count("backed_up")


def run_job(state, ctx) -> dict:
    cfg = state.cfg
    sysdef = state.system(ctx.params["system"])
    if not sysdef.disc:
        raise RuntimeError(f"{sysdef.key} is not a disc system in systems.yaml")
    input_path = Path(ctx.params["input"])
    games = discover(cfg, sysdef, input_path)
    only = set(ctx.params.get("only") or [])
    ctx.log(f"{len(games)} game(s) found in {input_path}")
    for g in games:
        if only and g.name not in only:
            continue
        ctx.check()
        if g.status == "present":
            ctx.count("present")
            ctx.item(g.name, "present", g.detail)
            continue
        if g.status in ("unsupported", "empty"):
            ctx.count(g.status)
            ctx.item(g.name, "unsupported", g.detail)
            ctx.log(f"UNSUPPORTED {g.name}: {g.detail}")
            continue
        if g.kind == "archive":
            ready = fsutil.wait_stable(g.path, cfg.stability.wait_seconds)
            if not ready.ok:
                ctx.count("failed")
                ctx.item(g.name, "failed", f"archive not ready: {ready.reason}")
                continue
        ctx.log(f"=== {g.name}")
        try:
            convert_game(ctx, cfg, sysdef, g)
        except Exception as e:  # noqa: BLE001 - keep going with the next game
            if _is_cancel(e):
                raise
            ctx.count("failed")
            ctx.item(g.name, "failed", f"{type(e).__name__}: {e}")
            ctx.log(f"FAIL {g.name}: {e}")
    fsutil.fix_perms(cfg.system_dir(sysdef.key), recursive=True)
    state.library.invalidate()
    before, after = ctx.counts.get("bytes_before", 0), ctx.counts.get("bytes_after", 0)
    return {"overall_ratio": f"{after / before:.1%}" if before else "n/a"}
