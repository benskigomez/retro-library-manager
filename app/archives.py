"""Archive reading: zip via zipfile (or Info-ZIP unzip), 7z via 7zz with py7zr fallback."""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil

ARCHIVE_EXTS = (".zip", ".7z")


@dataclass
class Entry:
    name: str        # path inside the archive, "/" separated
    size: int
    is_dir: bool = False
    crc: int | None = None

    @property
    def base(self) -> str:
        return self.name.rstrip("/").rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        return Path(self.base).suffix.lower()

    @property
    def stem(self) -> str:
        return Path(self.base).stem

    @property
    def in_subfolder(self) -> bool:
        return "/" in self.name.rstrip("/")


class Tools:
    sevenzip = "7zz"
    unzip = "unzip"
    zip_backend = "python"


TOOLS = Tools()


def configure_tools(sevenzip: str, unzip: str, zip_backend: str) -> None:
    TOOLS.sevenzip, TOOLS.unzip, TOOLS.zip_backend = sevenzip, unzip, zip_backend


def _sevenzip_bin() -> str | None:
    for cand in (TOOLS.sevenzip, "7zz", "7z", "7za"):
        if cand and shutil.which(cand):
            return cand
    return None


# ---- listing -----------------------------------------------------------------

def list_zip(path: Path) -> list[Entry]:
    with zipfile.ZipFile(path) as zf:
        return [Entry(i.filename, i.file_size, i.is_dir(), i.CRC) for i in zf.infolist()]


def list_7z(path: Path) -> list[Entry]:
    exe = _sevenzip_bin()
    if exe:
        out = subprocess.run([exe, "l", "-slt", "-ba", "-sccUTF-8", str(path)],
                             capture_output=True, check=True).stdout.decode("utf-8", "replace")
        entries, cur = [], {}
        for line in out.splitlines() + [""]:
            if not line.strip():
                if "Path" in cur:
                    entries.append(Entry(
                        cur["Path"].replace("\\", "/"),
                        int(cur.get("Size") or 0),
                        cur.get("Folder") == "+" or "D" in cur.get("Attributes", "")[:1],
                        int(cur["CRC"], 16) if cur.get("CRC") else None,
                    ))
                cur = {}
                continue
            k, _, v = line.partition(" = ")
            cur[k.strip()] = v
        return entries
    import py7zr

    with py7zr.SevenZipFile(path, "r") as z:
        return [Entry(i.filename.replace("\\", "/"), i.uncompressed or 0, i.is_directory, i.crc32)
                for i in z.list()]


def list_dir(path: Path) -> list[Entry]:
    """A folder used as a source: every non-hidden file, "/"-separated relative names."""
    path = Path(path)
    out = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = Path(root) / f
            out.append(Entry(str(p.relative_to(path)).replace("\\", "/"), p.stat().st_size))
    return out


def list_entries(path: Path) -> list[Entry]:
    if Path(path).is_dir():
        return list_dir(path)
    suffix = Path(path).suffix.lower()
    if suffix == ".zip":
        return list_zip(path)
    if suffix == ".7z":
        return list_7z(path)
    raise ValueError(f"unsupported archive type: {path}")


# ---- extraction --------------------------------------------------------------

def extract_zip_member(archive: Path, member: str, dest: Path, check=None) -> int:
    """Extract one zip member to an exact destination file (flat), never overwriting."""
    if TOOLS.zip_backend == "unzip" and shutil.which(TOOLS.unzip):
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(dest)
        proc = subprocess.Popen([TOOLS.unzip, "-p", str(archive), fsutil.escape_unzip_pattern(member)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        n = fsutil.write_stream_new(proc.stdout, dest, check)
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if proc.wait() != 0:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"unzip failed for {member}: {err.strip()}")
        return n
    with zipfile.ZipFile(archive) as zf, zf.open(member) as src:
        return fsutil.write_stream_new(src, dest, check)


def extract_all(archive: Path, dest_dir: Path) -> None:
    """Extract a whole archive into a (temp) directory, refusing path traversal."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(archive).suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                target = dest_dir / info.filename
                if not fsutil.is_within(target, dest_dir):
                    raise ValueError(f"unsafe path in archive: {info.filename}")
            zf.extractall(dest_dir)
        return
    if suffix == ".7z":
        exe = _sevenzip_bin()
        if exe:
            proc = subprocess.run([exe, "x", "-y", "-bd", f"-o{dest_dir}", str(archive)],
                                  capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "7z extraction failed")
        else:
            import py7zr

            with py7zr.SevenZipFile(archive, "r") as z:
                for name in z.getnames():
                    if not fsutil.is_within(dest_dir / name, dest_dir):
                        raise ValueError(f"unsafe path in archive: {name}")
                z.extractall(dest_dir)
        for root, _d, files in os.walk(dest_dir):
            for f in files:
                if not fsutil.is_within(Path(root) / f, dest_dir):
                    raise ValueError(f"unsafe path in archive: {f}")
        return
    raise ValueError(f"unsupported archive type: {archive}")


def stream_member(archive: Path, member: str, chunk: int = 1024 * 1024):
    """Yield one member's bytes without writing anything to disk (7z: via `7zz -so`)."""
    archive = Path(archive)
    if archive.is_dir():
        target = archive / member
        if not fsutil.is_within(target, archive):
            raise ValueError(f"unsafe path: {member}")
        with open(target, "rb") as f:
            while data := f.read(chunk):
                yield data
        return
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as zf, zf.open(member) as src:
            while data := src.read(chunk):
                yield data
        return
    exe = _sevenzip_bin()
    if exe:
        # -spd: no wildcard matching, so names with * ? [ ] are taken literally
        proc = subprocess.Popen([exe, "e", "-so", "-spd", "-bd", str(archive), "--", member],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert proc.stdout is not None
        try:
            while data := proc.stdout.read(chunk):
                yield data
        finally:
            if proc.poll() is None:
                proc.kill()  # the reader stopped early (e.g. read_member with a limit)
            proc.stdout.close()
            proc.wait()
        return
    import tempfile

    import py7zr

    with tempfile.TemporaryDirectory(prefix="rlm-7z-") as tmp:
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extract(path=tmp, targets=[member])
        target = Path(tmp) / member
        if not fsutil.is_within(target, tmp):
            raise ValueError(f"unsafe path in archive: {member}")
        with open(target, "rb") as f:
            while data := f.read(chunk):
                yield data


def read_member(source: Path, member: str, limit: int = 1024 * 1024) -> bytes:
    """The first ``limit`` bytes of a member (headers, .cue sheets)."""
    buf = bytearray()
    gen = stream_member(source, member, chunk=min(limit, 1024 * 1024))
    try:
        for data in gen:
            buf += data
            if len(buf) >= limit:
                break
    finally:
        gen.close()
    return bytes(buf[:limit])


# ---- inspection --------------------------------------------------------------

@dataclass
class Inspection:
    path: str
    size: int
    entry_count: int
    file_count: int
    in_subfolders: int
    inner_zip: int
    inner_7z: int
    top_extensions: list[tuple[str, int]]
    samples: list[str]
    inner_sample_names: list[str] = field(default_factory=list)


def inspect(path: Path, sample: int = 12) -> Inspection:
    entries = list_entries(path)
    files = [e for e in entries if not e.is_dir]
    exts = Counter(e.ext or "(none)" for e in files)
    inner_names: list[str] = []
    # Peek into a few inner zips so detection works on zip-of-zips sets.
    if Path(path).suffix.lower() == ".zip":
        inner = [e for e in files if e.ext == ".zip"][:5]
        if inner:
            with zipfile.ZipFile(path) as zf:
                for e in inner:
                    try:
                        with zf.open(e.name) as fh:
                            import io

                            data = io.BytesIO(fh.read(64 * 1024 * 1024))
                        with zipfile.ZipFile(data) as izf:
                            inner_names.extend(izf.namelist())
                    except (zipfile.BadZipFile, OSError):
                        continue
    step = max(1, len(files) // sample) if files else 1
    return Inspection(
        path=str(path),
        size=Path(path).stat().st_size,
        entry_count=len(entries),
        file_count=len(files),
        in_subfolders=sum(1 for e in files if e.in_subfolder),
        inner_zip=exts.get(".zip", 0),
        inner_7z=exts.get(".7z", 0),
        top_extensions=exts.most_common(10),
        samples=[e.name for e in files[::step][:sample]],
        inner_sample_names=inner_names[:20],
    )
