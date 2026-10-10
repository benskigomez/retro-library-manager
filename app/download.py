"""Downloading games to the operator's device.

Read-only with respect to the library: files are streamed from the system
folder. Multi-file games (.cue + tracks, .gdi, .m3u) are zipped on the fly.
CHDs can be converted back to BIN/CUE or ISO by a job into a temp folder
(cleaned after 24 h), then downloaded from there.
"""
from __future__ import annotations

import os
import shlex
import shutil
import time
import zipfile
from pathlib import Path

from . import chd, fsutil

PREPARED_TTL = 24 * 3600


def resolve(folder: Path, name: str) -> Path:
    p = Path(folder) / name
    if "/" in name or "\\" in name or not fsutil.is_within(p, folder) or not p.is_file():
        raise FileNotFoundError(name)
    return p


def game_files(path: Path) -> list[Path]:
    """The file plus anything it references (tracks of a .cue/.gdi, discs of an .m3u)."""
    path = Path(path)
    ext = path.suffix.lower()
    files = [path]
    if ext in (".cue", ".gdi"):
        tracks, _missing = chd.resolve_tracks(path)
        files += tracks
    elif ext == ".m3u":
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            disc = path.parent / line.replace("\\", "/")
            if disc.is_file() and fsutil.is_within(disc, path.parent):
                files += game_files(disc)
    seen, out = set(), []
    for f in files:
        key = str(f.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


class _Sink:
    """File-like object for zipfile that hands written bytes to a generator."""

    def __init__(self):
        self.chunks: list[bytes] = []
        self.pos = 0

    def write(self, b) -> int:
        self.chunks.append(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self.chunks)
        self.chunks.clear()
        return data


def stream_zip(files: list[Path], base: Path, chunk: int = 1024 * 1024):
    """Yield a ZIP (stored, no compression: ROMs/discs barely compress) without a temp file."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in files:
            arc = str(f.relative_to(base)).replace("\\", "/") if fsutil.is_within(f, base) else f.name
            info = zipfile.ZipInfo.from_file(f, arc)
            info.compress_type = zipfile.ZIP_STORED
            with open(f, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                while data := src.read(chunk):
                    dst.write(data)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


# ---- CHD conversion for download -------------------------------------------------

def prepared_root(cfg) -> Path:
    return cfg.temp_dir / "downloads"


def cleanup_prepared(cfg, now: float | None = None) -> int:
    root = prepared_root(cfg)
    if not root.is_dir():
        return 0
    now = now or time.time()
    removed = 0
    for d in root.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > PREPARED_TTL:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def prepared_files(cfg, job_id: int) -> list[Path]:
    d = prepared_root(cfg) / f"job-{job_id}"
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")), key=lambda p: p.name)


def run_convert(state, ctx) -> dict:
    """Job: chdman extractcd/extractdvd a CHD into the temp download area."""
    cfg = state.cfg
    cleanup_prepared(cfg)
    sysdef = state.system(ctx.params["system"])
    src = resolve(cfg.system_dir(sysdef.key), ctx.params["name"])
    fmt = ctx.params["format"]
    out_dir = prepared_root(cfg) / f"job-{ctx.id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    base_cmd = shlex.split(cfg.tools.chdman)
    if fmt == "iso":
        cmd = base_cmd + ["extractdvd", "-i", str(src), "-o", str(out_dir / f"{stem}.iso")]
    elif fmt == "cue":
        cmd = base_cmd + ["extractcd", "-i", str(src), "-o", str(out_dir / f"{stem}.cue"),
                          "-ob", str(out_dir / f"{stem}.bin")]
    else:
        raise ValueError(f"unknown format {fmt}")
    ctx.log(f"Converting {src.name} to {fmt.upper()} in {out_dir} (kept for 24 hours)")
    try:
        code, output = chd.run_chdman(ctx, cmd)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    if code != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise RuntimeError(f"chdman exited with {code}: {output[-2000:]}")
    files = prepared_files(cfg, ctx.id)
    total = sum(f.stat().st_size for f in files)
    for f in files:
        ctx.item(f.name, "added", f"{f.stat().st_size:,} bytes")
    ctx.log(f"Ready: {len(files)} file(s), {total:,} bytes. Use the Download button on this page.")
    return {"download": f"/download/prepared/{ctx.id}", "bytes": total}


def dir_age_ok(path: Path) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime <= PREPARED_TTL
    except OSError:
        return False
