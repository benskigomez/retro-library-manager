"""Filesystem primitives. Every write the app makes goes through here.

Safety rules enforced:
* never overwrite an existing file (``FileExistsError`` / skip)
* removals go to /roms/_trash/<timestamp>/<relative path>
* moves across filesystems are copy -> verify -> remove
* files written get nobody:users (when running as root) and a+rwX
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

CHUNK = 1024 * 1024


class Ownership:
    apply = True
    uid = 99
    gid = 100


OWNERSHIP = Ownership()


def configure_ownership(apply: bool, uid: int, gid: int) -> None:
    OWNERSHIP.apply, OWNERSHIP.uid, OWNERSHIP.gid = apply, uid, gid


def _fix_one(p: Path) -> None:
    try:
        st = p.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    # Skip syscalls when nothing would change: recursive passes over big
    # folders on Unraid's FUSE layer are otherwise slow.
    if hasattr(os, "geteuid") and os.geteuid() == 0 and (st.st_uid, st.st_gid) != (OWNERSHIP.uid, OWNERSHIP.gid):
        try:
            os.chown(p, OWNERSHIP.uid, OWNERSHIP.gid)
        except OSError:
            pass
    old = stat.S_IMODE(st.st_mode)
    mode = old | 0o666
    if stat.S_ISDIR(st.st_mode) or st.st_mode & 0o111:
        mode |= 0o111  # the "X" in a+rwX
    if mode != old:
        try:
            os.chmod(p, mode)
        except OSError:
            pass


def fix_perms(path: Path, recursive: bool = False) -> None:
    """chown nobody:users (if root) and chmod a+rwX."""
    if not OWNERSHIP.apply:
        return
    path = Path(path)
    _fix_one(path)
    if recursive and path.is_dir():
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                _fix_one(Path(root) / n)


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        fix_perms(path)
    return path


def md5_file(path: Path, progress: Callable[[], None] | None = None) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            if progress:
                progress()
    return h.hexdigest()


def md5_stream(fh: BinaryIO) -> str:
    h = hashlib.md5()
    while chunk := fh.read(CHUNK):
        h.update(chunk)
    return h.hexdigest()


def write_stream_new(src: BinaryIO, dest: Path, check: Callable[[], None] | None = None) -> int:
    """Write a stream to ``dest`` via a temp name; refuse to overwrite. Returns bytes."""
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(dest)
    tmp = dest.with_name(f".rlm-part-{os.getpid()}-{dest.name}")
    total = 0
    try:
        with open(tmp, "xb") as out:
            while chunk := src.read(CHUNK):
                out.write(chunk)
                total += len(chunk)
                if check:
                    check()
        _publish(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fix_perms(dest)
    return total


def _publish(tmp: Path, dest: Path) -> None:
    """Rename tmp -> dest without ever clobbering an existing dest."""
    try:
        os.link(tmp, dest)  # atomic and fails if dest exists
        tmp.unlink()
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno == errno.EEXIST:
            raise FileExistsError(dest) from e
        # Filesystems without hard links (some SMB/FUSE setups): check then rename.
        if dest.exists():
            raise FileExistsError(dest) from e
        os.replace(tmp, dest)


def copy_new(src: Path, dest: Path, check: Callable[[], None] | None = None) -> int:
    with open(src, "rb") as fh:
        n = write_stream_new(fh, dest, check)
    try:
        shutil.copystat(src, dest)
    except OSError:
        pass
    fix_perms(dest)
    return n


def copy_verify(src: Path, dest: Path, check: Callable[[], None] | None = None) -> str:
    """Copy without overwrite, then verify size and MD5. Returns the MD5."""
    copy_new(src, dest, check)
    src_md5, dst_md5 = md5_file(src), md5_file(dest)
    if src.stat().st_size != dest.stat().st_size or src_md5 != dst_md5:
        dest.unlink(missing_ok=True)
        raise IOError(f"verification failed copying {src} -> {dest}")
    return dst_md5


def same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def safe_move(src: Path, dest: Path, check: Callable[[], None] | None = None) -> None:
    """Move without overwrite. Same filesystem: rename. Otherwise copy-verify-remove.

    On Unraid, /mnt/user shares are separate FUSE trees; a rename between them
    is never attempted (copy, verify MD5, then remove the source).
    """
    src, dest = Path(src), Path(dest)
    if dest.exists():
        raise FileExistsError(dest)
    ensure_dir(dest.parent)
    if src.is_dir():
        if same_device(src, dest.parent) and _same_top(src, dest):
            os.rename(src, dest)
            return
        for root, _dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            ensure_dir(dest / rel)
            for f in files:
                copy_verify(Path(root) / f, dest / rel / f, check)
        shutil.rmtree(src)
        return
    if same_device(src, dest.parent) and _same_top(src, dest):
        try:
            os.link(src, dest)
            os.unlink(src)
            return
        except OSError:
            if dest.exists():
                raise FileExistsError(dest)
            os.rename(src, dest)
            return
    copy_verify(src, dest, check)
    src.unlink()


def _same_top(a: Path, b: Path) -> bool:
    """True unless the two paths live in different /mnt/user/<share> trees."""
    pa, pb = Path(a).resolve().parts, Path(b).resolve().parts
    if len(pa) > 3 and len(pb) > 3 and pa[1:3] == ("mnt", "user"):
        return pa[3] == pb[3]
    return True


# ---- trash -----------------------------------------------------------------

def new_trash_batch(trash_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = trash_root / stamp
    n = 1
    while batch.exists():
        n += 1
        batch = trash_root / f"{stamp}-{n}"
    return ensure_dir(batch)


def move_to_trash(path: Path, roms_root: Path, batch: Path) -> Path:
    """Move a file/dir under roms_root into the trash batch, keeping its relative path."""
    rel = Path(path).resolve().relative_to(Path(roms_root).resolve())
    dest = batch / rel
    safe_move(path, dest)
    return dest


def restore_from_trash(batch: Path, roms_root: Path) -> tuple[list[str], list[str]]:
    """Restore every file in a trash batch. Returns (restored, skipped_existing)."""
    restored, skipped = [], []
    for root, _dirs, files in os.walk(batch):
        for f in files:
            src = Path(root) / f
            rel = src.relative_to(batch)
            dest = Path(roms_root) / rel
            if dest.exists():
                skipped.append(str(rel))
                continue
            safe_move(src, dest)
            restored.append(str(rel))
    _prune_empty(batch)
    return restored, skipped


def _prune_empty(root: Path) -> None:
    for d, _dirs, _files in sorted(os.walk(root), key=lambda t: -len(t[0])):
        try:
            os.rmdir(d)
        except OSError:
            pass


def tree_size(path: Path) -> tuple[int, int]:
    """(bytes, file count) under path."""
    total = count = 0
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count


# ---- archive readiness -------------------------------------------------------

@dataclass
class Readiness:
    ok: bool
    reason: str = ""


def wait_stable(path: Path, wait_seconds: float, sleep: Callable[[float], None] = time.sleep) -> Readiness:
    """An archive still being copied over SMB can already report its full size.

    Require identical size and mtime across two checks ``wait_seconds`` apart,
    then require the container's index (zip central directory / 7z header) to parse.
    """
    path = Path(path)
    if path.is_dir():
        def snapshot():
            out = []
            for root, _d, files in os.walk(path):
                for f in files:
                    try:
                        st = os.stat(os.path.join(root, f))
                    except FileNotFoundError:
                        continue
                    out.append((root, f, st.st_size, st.st_mtime_ns))
            return sorted(out)

        first = snapshot()
        sleep(wait_seconds)
        if snapshot() != first:
            return Readiness(False, "files in the folder are still changing; try again when the copy finishes")
        if not first:
            return Readiness(False, "folder is empty")
        return Readiness(True)
    try:
        a = path.stat()
        sleep(wait_seconds)
        b = path.stat()
    except FileNotFoundError:
        return Readiness(False, "file disappeared")
    if (a.st_size, a.st_mtime_ns) != (b.st_size, b.st_mtime_ns):
        return Readiness(False, "file is still changing (size or mtime moved); try again when the copy finishes")
    if a.st_size == 0:
        return Readiness(False, "file is empty")
    suffix = path.suffix.lower()
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                zf.infolist()
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            return Readiness(False, f"zip central directory does not parse: {e}")
    elif suffix == ".7z":
        with open(path, "rb") as f:
            if f.read(6) != b"7z\xbc\xaf\x27\x1c":
                return Readiness(False, "not a 7z archive (bad signature)")
        from .archives import list_7z  # local import: avoids a cycle

        try:
            list_7z(path)
        except Exception as e:  # noqa: BLE001 - surface any parser/tool error
            return Readiness(False, f"7z header does not parse: {e}")
    return Readiness(True)


# ---- unzip helpers -----------------------------------------------------------

def escape_unzip_pattern(name: str) -> str:
    """Info-ZIP unzip treats member names as wildcards; escape [ ] * ? and backslash."""
    out = []
    for ch in name:
        if ch in "[]*?\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def is_within(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False
