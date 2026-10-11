"""Content-based duplicate detection: is this exact ROM already in the library under any name?

Matching is by CRC32 + size. Incoming CRCs come free from zip/7z listings. Library
files are hashed only when their size equals an incoming file's size, and the CRCs
are cached in SQLite keyed by (path, size, mtime), so repeat checks are cheap.
"""
from __future__ import annotations

import os
import zlib
from pathlib import Path

SCHEMA = "CREATE TABLE IF NOT EXISTS crc_cache (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, crc INTEGER)"
META = {"gamelist.xml"}


def crc32_file(path: Path, chunk: int = 1024 * 1024) -> int:
    crc = 0
    with open(path, "rb") as f:
        while data := f.read(chunk):
            crc = zlib.crc32(data, crc)
    return crc & 0xFFFFFFFF


class ContentIndex:
    """CRC32/size lookup over the files directly inside one system folder."""

    def __init__(self, db, folder: Path):
        self.db = db
        self.folder = Path(folder)
        self._by_size: dict[int, list[Path]] | None = None
        self._added: dict[tuple[int, int], str] = {}
        if db is not None:
            db.execute(SCHEMA)

    def _sizes(self) -> dict[int, list[Path]]:
        if self._by_size is None:
            self._by_size = {}
            if self.folder.is_dir():
                for e in os.scandir(self.folder):
                    if not e.is_file(follow_symlinks=False) or e.name.startswith(".") or e.name.lower() in META:
                        continue
                    if e.name.lower().startswith("gamelist.xml"):
                        continue
                    self._by_size.setdefault(e.stat().st_size, []).append(Path(e.path))
        return self._by_size

    def _crc(self, path: Path) -> int | None:
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if self.db is not None:
            row = self.db.one("SELECT size, mtime, crc FROM crc_cache WHERE path=?", (str(path),))
            if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime_ns:
                return row["crc"]
        crc = crc32_file(path)
        if self.db is not None:
            self.db.execute("INSERT OR REPLACE INTO crc_cache(path, size, mtime, crc) VALUES (?,?,?,?)",
                            (str(path), st.st_size, st.st_mtime_ns, crc))
        return crc

    def has_size(self, size: int) -> bool:
        return size in self._sizes() or any(s == size for _c, s in self._added)

    def find(self, crc: int | None, size: int) -> str | None:
        """Name of a library file with exactly this content, or None."""
        if crc is None:
            return None
        crc &= 0xFFFFFFFF
        hit = self._added.get((crc, size))
        if hit:
            return hit
        for p in self._sizes().get(size, []):
            if self._crc(p) == crc:
                return p.name
        return None

    def add(self, name: str, crc: int | None, size: int) -> None:
        """Remember a file written during this run, so a later identical entry is caught too."""
        if crc is not None:
            self._added[(crc & 0xFFFFFFFF, size)] = name
