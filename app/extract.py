"""Import and extraction engine.

``plan()`` is the dry run: it decides every archive entry without writing
anything. ``run()`` (a job handler) re-plans against the current state of the
disk and then performs exactly the "include" and "bios" rows of that plan.
Re-runs are cheap: anything whose stem already exists is "present".
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import archives, fsutil
from .config import Config
from .filters import BIOS, EXCLUDE, INCLUDE, RuleSet
from .hooks import HOOKS
from .systems import ARCHIVE_EXTS, System

# Decisions shown in the dry run
PRESENT = "present"
SKIPPED = "skipped"

# Entry kinds
ROM, INNER, SET, OTHER = "rom", "inner", "set", "other"


@dataclass
class Row:
    entry: str
    base: str
    kind: str
    decision: str
    rule: str
    target: str = ""


@dataclass
class Plan:
    archive: str
    system: str
    rows: list[Row] = field(default_factory=list)

    @property
    def totals(self) -> Counter:
        return Counter(r.decision for r in self.rows)

    def by_decision(self, decision: str) -> list[Row]:
        return [r for r in self.rows if r.decision == decision]

    def csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["entry", "base", "kind", "decision", "rule", "target"])
        for r in self.rows:
            w.writerow([r.entry, r.base, r.kind, r.decision, r.rule, r.target])
        return buf.getvalue()


def existing_stems(folder: Path) -> set[str]:
    """Case-folded stems of files already in a system folder (dedupe key)."""
    if not folder.is_dir():
        return set()
    return {Path(e.name).stem.casefold() for e in os.scandir(folder) if e.is_file() and not e.name.startswith(".")}


def existing_names(folder: Path) -> set[str]:
    if not folder.is_dir():
        return set()
    return {e.name.casefold() for e in os.scandir(folder) if e.is_file()}


def plan(cfg: Config, sysdef: System, archive: Path, entries: list[archives.Entry] | None = None) -> Plan:
    archive = Path(archive)
    entries = entries if entries is not None else archives.list_entries(archive)
    rules = RuleSet(cfg.filters, sysdef.key)
    folder = cfg.system_dir(sysdef.key)
    bios_folder = cfg.bios_dir / sysdef.key
    stems = existing_stems(folder)
    names = existing_names(folder)
    bios_names = existing_names(bios_folder)
    ignore = {e.lower() for e in cfg.ignore_extensions}
    accepted = sysdef.rom_extensions - set(ARCHIVE_EXTS)
    seen: set[str] = set()
    p = Plan(str(archive), sysdef.key)

    for e in entries:
        if e.is_dir:
            continue
        base, ext, stem = e.base, e.ext, e.stem
        if ext in ignore:
            p.rows.append(Row(e.name, base, OTHER, SKIPPED, f"ignored file type {ext}"))
            continue
        bios_why = rules.bios_rule(base)
        if bios_why:
            if base.casefold() in bios_names:
                p.rows.append(Row(e.name, base, ROM, PRESENT, "BIOS already staged", f"_bios/{sysdef.key}/{base}"))
            elif "bios:" + base.casefold() in seen:
                p.rows.append(Row(e.name, base, ROM, SKIPPED, "duplicate name in archive"))
            else:
                seen.add("bios:" + base.casefold())
                p.rows.append(Row(e.name, base, ROM, BIOS, bios_why, f"_bios/{sysdef.key}/{base}"))
            continue

        if sysdef.arcade:
            if ext not in ARCHIVE_EXTS:
                p.rows.append(Row(e.name, base, OTHER, SKIPPED, f"not a set archive ({ext or 'no extension'})"))
                continue
            d = rules.decide_arcade(e.name)
            key = base.casefold()
            if d.category != INCLUDE:
                p.rows.append(Row(e.name, base, SET, d.category, d.rule))
            elif key in names:
                p.rows.append(Row(e.name, base, SET, PRESENT, "set already in folder", base))
            elif key in seen:
                p.rows.append(Row(e.name, base, SET, SKIPPED, "duplicate name in archive"))
            else:
                seen.add(key)
                p.rows.append(Row(e.name, base, SET, INCLUDE, d.rule, base))
            continue

        kind = INNER if ext in ARCHIVE_EXTS else ROM
        if kind == ROM and accepted and ext not in accepted:
            p.rows.append(Row(e.name, base, OTHER, SKIPPED, f"extension {ext or '(none)'} not accepted by {sysdef.key}"))
            continue
        d = rules.decide(e.name)
        key = stem.casefold()
        # Inside one archive, "Game.cue" and "Game.bin" are distinct files; only
        # identical base names are duplicates. Against the folder, the stem decides.
        dup_key = key if kind == INNER else base.casefold()
        if d.category == BIOS:  # custom bios prefix handled above; keep for completeness
            p.rows.append(Row(e.name, base, kind, BIOS, d.rule, f"_bios/{sysdef.key}/{base}"))
        elif d.category != INCLUDE:
            p.rows.append(Row(e.name, base, kind, EXCLUDE, d.rule))
        elif key in stems:
            p.rows.append(Row(e.name, base, kind, PRESENT, f"{stem}.* already exists"))
        elif dup_key in seen:
            p.rows.append(Row(e.name, base, kind, SKIPPED, "duplicate name in archive"))
        else:
            seen.add(dup_key)
            p.rows.append(Row(e.name, base, kind, INCLUDE, d.rule, base if kind == ROM else f"{stem}.*"))
    return p


# ---- execution -----------------------------------------------------------------

class Extractor:
    """Pulls single members out of the outer archive (zip directly, 7z via a temp tree)."""

    def __init__(self, archive: Path, tmp_root: Path):
        self.archive = Path(archive)
        self.is_zip = self.archive.suffix.lower() == ".zip"
        self.tmp_root = tmp_root
        self._tree: Path | None = None

    def write_member(self, member: str, dest: Path, check=None) -> int:
        if self.is_zip:
            return archives.extract_zip_member(self.archive, member, dest, check)
        if self._tree is None:
            self._tree = Path(tempfile.mkdtemp(prefix="outer-", dir=self.tmp_root))
            archives.extract_all(self.archive, self._tree)
        return fsutil.copy_new(self._tree / member, dest, check)

    def member_to_temp(self, member: str, check=None) -> Path:
        d = Path(tempfile.mkdtemp(prefix="inner-", dir=self.tmp_root))
        dest = d / Path(member).name
        self.write_member(member, dest, check)
        return dest

    def close(self) -> None:
        if self._tree is not None:
            shutil.rmtree(self._tree, ignore_errors=True)


def _unpack_inner(ctx, cfg: Config, sysdef: System, inner_archive: Path, folder: Path, row: Row) -> None:
    """Unpack one per-game inner archive's ROM files flat into the system folder."""
    work = inner_archive.parent / "x"
    archives.extract_all(inner_archive, work)
    ignore = {e.lower() for e in cfg.ignore_extensions}
    accepted = sysdef.rom_extensions - set(ARCHIVE_EXTS)
    roms = []
    for root, _d, files in os.walk(work):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in ignore:
                continue
            if accepted and ext not in accepted:
                ctx.log(f"  {row.base}: ignoring {f} (extension not accepted)")
                continue
            roms.append(Path(root) / f)
    if not roms:
        ctx.count("skipped")
        ctx.item(row.base, "skipped", "inner archive has no ROM files for this system")
        ctx.log(f"SKIP {row.base}: no ROM files inside")
        return
    before = set(existing_stems_cache(ctx, folder))  # siblings from this archive may share a stem
    for src in roms:
        dest = folder / src.name
        if dest.exists() or Path(src.name).stem.casefold() in before:
            ctx.count("present")
            ctx.item(src.name, "present", f"from {row.base}: already exists")
            continue
        fsutil.copy_new(src, dest, ctx.check)
        existing_stems_cache(ctx, folder).add(Path(src.name).stem.casefold())
        ctx.count("added")
        ctx.item(src.name, "added", f"from {row.base}")
        ctx.log(f"ADD  {src.name}  (from {row.base})")


def existing_stems_cache(ctx, folder: Path) -> set[str]:
    cache = getattr(ctx, "_stems", None)
    if cache is None:
        cache = existing_stems(folder)
        ctx._stems = cache
    return cache


def execute(ctx, cfg: Config, sysdef: System, archive: Path) -> dict:
    archive = Path(archive)
    ready = fsutil.wait_stable(archive, cfg.stability.wait_seconds)
    if not ready.ok:
        raise RuntimeError(f"{archive.name} is not ready: {ready.reason}")
    ctx.log(f"Archive {archive} is stable; planning against {cfg.system_dir(sysdef.key)}")
    p = plan(cfg, sysdef, archive)
    totals = p.totals
    ctx.log("Plan: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    ctx.count("matched", totals.get(INCLUDE, 0))
    ctx.count("present", totals.get(PRESENT, 0))
    ctx.count("excluded", totals.get(EXCLUDE, 0))
    for r in p.by_decision(SKIPPED):
        ctx.count("skipped")
        ctx.item(r.base, "skipped", r.rule)

    folder = fsutil.ensure_dir(cfg.system_dir(sysdef.key))
    bios_folder = cfg.bios_dir / sysdef.key
    tmp_root = fsutil.ensure_dir(cfg.temp_dir / f"job-{ctx.id}")
    ex = Extractor(archive, tmp_root)
    try:
        for r in p.rows:
            if r.decision not in (INCLUDE, BIOS):
                continue
            ctx.check()
            try:
                if r.decision == BIOS:
                    fsutil.ensure_dir(bios_folder)
                    ex.write_member(r.entry, bios_folder / r.base, ctx.check)
                    ctx.count("bios_staged")
                    ctx.item(r.base, "bios", f"staged to _bios/{sysdef.key}/")
                    ctx.log(f"BIOS {r.base} -> _bios/{sysdef.key}/")
                elif r.kind in (ROM, SET):
                    ex.write_member(r.entry, folder / r.base, ctx.check)
                    existing_stems_cache(ctx, folder).add(Path(r.base).stem.casefold())
                    ctx.count("added")
                    ctx.item(r.base, "added", r.rule)
                    ctx.log(f"ADD  {r.base}")
                elif r.kind == INNER:
                    stem = Path(r.base).stem.casefold()
                    if stem in existing_stems_cache(ctx, folder):
                        ctx.count("present")
                        continue
                    inner = ex.member_to_temp(r.entry, ctx.check)
                    try:
                        _unpack_inner(ctx, cfg, sysdef, inner, folder, r)
                    finally:
                        shutil.rmtree(inner.parent, ignore_errors=True)
            except FileExistsError as e:
                ctx.count("present")
                ctx.item(r.base, "present", f"not overwritten: {Path(str(e)).name}")
            except Exception as e:  # noqa: BLE001 - one bad entry must not stop the run
                if type(e).__name__ == "Cancelled":
                    raise
                ctx.count("failed")
                ctx.item(r.base, "failed", f"{type(e).__name__}: {e}")
                ctx.log(f"FAIL {r.base}: {e}")
    finally:
        ex.close()
        shutil.rmtree(tmp_root, ignore_errors=True)
        ctx.log("Applying ownership/permissions")
        fsutil.fix_perms(folder, recursive=True)
        if bios_folder.exists():
            fsutil.fix_perms(bios_folder, recursive=True)

    for hook in sysdef.hooks:
        fn = HOOKS.get(hook)
        if fn:
            ctx.log(f"Running {hook} hook")
            fn(ctx, cfg, folder)
            fsutil.fix_perms(folder, recursive=True)
    return {}


# ---- import into _source ---------------------------------------------------------

def source_name(system: str, archive: Path) -> str:
    return f"{system}-roms{Path(archive).suffix.lower()}"


def import_archive(ctx, cfg: Config, system: str, archive: Path, mode: str) -> Path:
    """Store an archive in _source as <system>-roms.<ext>. Returns the path to extract from.

    mode: "none" (extract in place), "copy", or "move" (copy-verify-remove).
    _source is never modified except for adding the new archive.
    """
    archive = Path(archive)
    if mode == "none":
        return archive
    dest = cfg.source_dir / source_name(system, archive)
    if archive.resolve() == dest.resolve():
        ctx.log(f"{archive.name} is already the source archive for {system}")
        return dest
    if fsutil.is_within(archive, cfg.source_dir) and mode == "move":
        raise RuntimeError("refusing to move a file that is already inside _source; use copy")
    if dest.exists():
        if dest.stat().st_size == archive.stat().st_size and fsutil.md5_file(dest) == fsutil.md5_file(archive):
            ctx.log(f"_source/{dest.name} already exists with identical content; using it")
            return dest
        raise RuntimeError(
            f"_source/{dest.name} already exists with different content; not overwriting. "
            "Extract without importing, or rename the existing archive yourself."
        )
    fsutil.ensure_dir(cfg.source_dir)
    ctx.log(f"Importing {archive} -> _source/{dest.name} ({mode})")
    if mode == "move":
        fsutil.safe_move(archive, dest, ctx.check)
    else:
        fsutil.copy_verify(archive, dest, ctx.check)
    ctx.log("Import verified (size + MD5)")
    ctx.count("imported")
    return dest


def run_job(state, ctx) -> dict:
    prm = ctx.params
    sysdef = state.system(prm["system"])
    archive = Path(prm["archive"])
    cfg = state.cfg
    if prm.get("import_mode", "none") != "none":
        ready = fsutil.wait_stable(archive, cfg.stability.wait_seconds)
        if not ready.ok:
            raise RuntimeError(f"{archive.name} is not ready: {ready.reason}")
        archive = import_archive(ctx, cfg, sysdef.key, archive, prm["import_mode"])
    result = execute(ctx, cfg, sysdef, archive)
    state.library.invalidate()
    return result


def plan_dict(p: Plan) -> dict:
    return {"archive": p.archive, "system": p.system, "rows": [asdict(r) for r in p.rows]}
