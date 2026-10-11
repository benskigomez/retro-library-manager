"""Application state shared by routes and job handlers."""
from __future__ import annotations

from pathlib import Path

from . import archives, fsutil
from .config import Config, Settings
from .db import Database
from .jobs import JobQueue
from .library import LibraryCache
from .systems import System, load_systems


class State:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.apply_runtime_config()
        self.db = Database(self.cfg.manager_dir / "rlm.db")
        self.queue = JobQueue(self.db, self.cfg.logs_dir, self.cfg.jobs.workers)
        self.library = LibraryCache()
        self._systems: dict[str, System] | None = None

    @property
    def cfg(self) -> Config:
        return self.settings.config

    @property
    def systems(self) -> dict[str, System]:
        if self._systems is None:
            self._systems = load_systems(self.settings.systems_path)
        return self._systems

    def system(self, key: str) -> System:
        try:
            return self.systems[key]
        except KeyError:
            raise KeyError(f"unknown system '{key}' (not in systems.yaml)") from None

    def reload(self) -> None:
        self.settings.reload()
        self._systems = None
        self.apply_runtime_config()
        self.library.invalidate()

    def apply_runtime_config(self) -> None:
        c = self.settings.config
        fsutil.configure_ownership(c.ownership.apply, c.ownership.uid, c.ownership.gid)
        archives.configure_tools(c.tools.sevenzip, c.tools.unzip, c.tools.zip_backend)

    def content_index(self, system: str):
        """CRC32/size index of a system folder, for duplicate-content checks in dry runs."""
        from .dedupe import ContentIndex

        return ContentIndex(self.db, self.cfg.system_dir(system))

    def browse_roots(self) -> list[Path]:
        roots = [self.cfg.roms] + [Path(p) for p in self.cfg.paths.browse_roots]
        return [r for r in roots if str(r)]

    def check_browsable(self, path: str | Path) -> Path:
        p = Path(path)
        if not any(fsutil.is_within(p, r) for r in self.browse_roots()):
            raise PermissionError(f"{p} is outside the allowed folders")
        return p
