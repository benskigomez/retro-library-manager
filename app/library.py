"""Read-only scan of the existing library for the dashboard."""
from __future__ import annotations

import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from .config import Config
from .systems import ARCHIVE_EXTS, System

# Files RetroBat/ES keep in system folders that are not games.
META_FILES = {"gamelist.xml", "_info.txt", "systeminfo.txt"}
TRACK_EXTS = {".bin", ".img", ".sub", ".raw", ".wav"}


@dataclass
class SystemStatus:
    key: str
    name: str
    exists: bool
    game_count: int = 0
    file_count: int = 0
    bytes: int = 0
    source_archives: list[str] = field(default_factory=list)
    source_backups: int = 0
    bios_staged: int = 0
    unrecognized: list[tuple[str, int]] = field(default_factory=list)
    archives_unexpected: int = 0
    disc: str | None = None
    arcade: bool = False

    @property
    def has_source(self) -> bool:
        return bool(self.source_archives) or self.source_backups > 0

    @property
    def flagged(self) -> bool:
        return bool(self.unrecognized) or self.archives_unexpected > 0


def _is_meta(name: str) -> bool:
    low = name.lower()
    return low in META_FILES or low.startswith("gamelist.xml") or name.startswith(".")


def scan_system(cfg: Config, sysdef: System) -> SystemStatus:
    folder = cfg.system_dir(sysdef.key)
    st = SystemStatus(sysdef.key, sysdef.name, folder.is_dir(), disc=sysdef.disc, arcade=sysdef.arcade)
    accepted = sysdef.rom_extensions
    ignore = {e.lower() for e in cfg.ignore_extensions}
    if st.exists:
        stems: set[str] = set()
        unknown: Counter[str] = Counter()
        try:
            with os.scandir(folder) as it:
                for e in it:
                    if not e.is_file(follow_symlinks=False) or _is_meta(e.name):
                        continue
                    ext = Path(e.name).suffix.lower()
                    if ext in accepted:
                        if sysdef.disc and ext in TRACK_EXTS:
                            continue
                        stems.add(Path(e.name).stem.casefold())
                    elif ext in ARCHIVE_EXTS and not sysdef.arcade:
                        st.archives_unexpected += 1
                    elif ext not in ignore:
                        unknown[ext or "(none)"] += 1
        except OSError:
            pass
        # Multi-disc / folder-per-game layouts (e.g. "Game.m3u" dirs) count as one game each.
        st.game_count = len(stems)
        st.unrecognized = unknown.most_common()
        st.bytes, st.file_count = fsutil.tree_size(folder)
    for ext in (".zip", ".7z"):
        p = cfg.source_dir / f"{sysdef.key}-roms{ext}"
        if p.is_file():
            st.source_archives.append(p.name)
    backups = cfg.source_dir / sysdef.key
    if backups.is_dir():
        st.source_backups = sum(1 for e in os.scandir(backups) if e.is_file())
    bios = cfg.bios_dir / sysdef.key
    if bios.is_dir():
        st.bios_staged = sum(1 for _ in bios.rglob("*") if _.is_file())
    return st


@dataclass
class LibraryScan:
    systems: list[SystemStatus]
    unknown_folders: list[str]
    scanned_at: float

    @property
    def missing_sources(self) -> list[SystemStatus]:
        return [s for s in self.systems if s.game_count > 0 and not s.has_source]


def scan_library(cfg: Config, systems: dict[str, System]) -> LibraryScan:
    statuses = [scan_system(cfg, s) for _key, s in sorted(systems.items(), key=lambda kv: kv[0].casefold())]
    unknown: list[str] = []
    if cfg.roms.is_dir():
        for e in os.scandir(cfg.roms):
            if e.is_dir() and not e.name.startswith(("_", ".")) and e.name not in systems:
                unknown.append(e.name)
    return LibraryScan(statuses, sorted(unknown), time.time())


class LibraryCache:
    def __init__(self, ttl: float = 300):
        self.ttl = ttl
        self._scan: LibraryScan | None = None
        self._lock = threading.Lock()

    def get(self, cfg: Config, systems: dict[str, System], force: bool = False) -> LibraryScan:
        with self._lock:
            if force or not self._scan or time.time() - self._scan.scanned_at > self.ttl:
                self._scan = scan_library(cfg, systems)
            return self._scan

    def invalidate(self) -> None:
        with self._lock:
            self._scan = None
