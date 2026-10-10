"""SQLite storage for jobs (and, separately, MAME reference data).

The DB lives under /roms/_manager so it rides along with the share backup.
Unraid user shares are FUSE; rollback journal mode is used instead of WAL.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    system      TEXT,
    params      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|cancelled|interrupted
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at  TEXT,
    ended_at    TEXT,
    summary     TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    cancel      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_system ON jobs(system);
CREATE TABLE IF NOT EXISTS job_items (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    status  TEXT NOT NULL,
    detail  TEXT,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS job_items_job ON job_items(job_id);
"""


class Database:
    def __init__(self, path: Path, schema: str = SCHEMA):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(schema)
        self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
