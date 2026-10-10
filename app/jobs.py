"""Persistent background job queue.

Jobs are rows in SQLite; worker threads (one by default) pick the oldest queued
job and run its registered handler. Each job writes a plain-text log file under
/roms/_manager/logs which the browser follows over Server-Sent Events.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .db import Database, dumps

TERMINAL = {"done", "failed", "cancelled", "interrupted"}


class Cancelled(Exception):
    pass


class JobContext:
    def __init__(self, queue: "JobQueue", job_id: int, params: dict[str, Any]):
        self.queue = queue
        self.id = job_id
        self.params = params
        self.counts: Counter[str] = Counter()
        self._log = open(queue.log_path(job_id), "a", encoding="utf-8", buffering=1)
        self._last_cancel_check = 0.0
        self._cancelled = False

    # logging -------------------------------------------------------------
    def log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in str(msg).splitlines() or [""]:
            self._log.write(f"[{stamp}] {line}\n")

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def item(self, name: str, status: str, detail: str = "", **data: Any) -> None:
        self.queue.db.execute(
            "INSERT INTO job_items(job_id, name, status, detail, data) VALUES (?,?,?,?,?)",
            (self.id, name, status, detail, dumps(data)),
        )

    # cancellation ----------------------------------------------------------
    def check(self) -> None:
        """Raise Cancelled if the operator asked to stop. Cheap to call often."""
        now = time.monotonic()
        if not self._cancelled and now - self._last_cancel_check > 0.5:
            self._last_cancel_check = now
            row = self.queue.db.one("SELECT cancel FROM jobs WHERE id=?", (self.id,))
            self._cancelled = bool(row and row["cancel"])
        if self._cancelled:
            raise Cancelled()

    def close(self) -> None:
        self._log.close()


Handler = Callable[[JobContext], dict | None]


class JobQueue:
    def __init__(self, db: Database, logs_dir: Path, workers: int = 1):
        self.db = db
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.handlers: dict[str, Handler] = {}
        self.workers = workers
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._claim_lock = threading.Lock()

    def register(self, kind: str):
        def deco(fn: Handler) -> Handler:
            self.handlers[kind] = fn
            return fn

        return deco

    def log_path(self, job_id: int) -> Path:
        return self.logs_dir / f"job-{job_id:06d}.log"

    # API ---------------------------------------------------------------------
    def submit(self, kind: str, title: str, params: dict[str, Any], system: str | None = None) -> int:
        if kind not in self.handlers:
            raise KeyError(f"unknown job kind {kind}")
        job_id = self.db.execute(
            "INSERT INTO jobs(kind, title, system, params) VALUES (?,?,?,?)",
            (kind, title, system, dumps(params)),
        )
        self._wake.set()
        return job_id

    def cancel(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
        self.db.execute(
            "UPDATE jobs SET status='cancelled', ended_at=datetime('now') WHERE id=? AND status='queued'",
            (job_id,),
        )

    def get(self, job_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _row(row) if row else None

    def items(self, job_id: int) -> list[dict]:
        rows = self.db.query("SELECT * FROM job_items WHERE job_id=? ORDER BY id", (job_id,))
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]

    def recent(self, limit: int = 100, system: str | None = None) -> list[dict]:
        if system:
            rows = self.db.query("SELECT * FROM jobs WHERE system=? ORDER BY id DESC LIMIT ?", (system, limit))
        else:
            rows = self.db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]

    def last_by_system(self) -> dict[str, dict]:
        rows = self.db.query(
            "SELECT * FROM jobs WHERE id IN (SELECT max(id) FROM jobs WHERE system IS NOT NULL GROUP BY system)"
        )
        return {r["system"]: _row(r) for r in rows}

    # worker --------------------------------------------------------------------
    def start(self) -> None:
        self.db.execute(
            "UPDATE jobs SET status='interrupted', ended_at=datetime('now'), "
            "error='app restarted while job was running' WHERE status='running'"
        )
        self._stop.clear()
        for n in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"job-worker-{n}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()

    def _claim(self) -> dict | None:
        with self._claim_lock:
            row = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not row:
                return None
            self.db.execute(
                "UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?", (row["id"],)
            )
            return _row(row)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._claim()
            if not job:
                self._wake.wait(2)
                self._wake.clear()
                continue
            self.run_job(job)

    def run_job(self, job: dict) -> None:
        ctx = JobContext(self, job["id"], job["params"])
        status, error, extra = "done", None, None
        ctx.log(f"Job #{job['id']} {job['kind']}: {job['title']}")
        try:
            extra = self.handlers[job["kind"]](ctx)
        except Cancelled:
            status = "cancelled"
            ctx.log("Cancelled by operator.")
        except Exception as e:  # noqa: BLE001 - job failures are reported, not raised
            status, error = "failed", f"{type(e).__name__}: {e}"
            ctx.log("FAILED: " + error)
            ctx.log(traceback.format_exc())
        summary = dict(ctx.counts)
        if extra:
            summary.update(extra)
        ctx.log(f"Finished with status {status}. Summary: {json.dumps(summary, default=str)}")
        ctx.close()
        self.db.execute(
            "UPDATE jobs SET status=?, ended_at=datetime('now'), summary=?, error=? WHERE id=?",
            (status, dumps(summary), error, job["id"]),
        )


def _row(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.get("params") or "{}")
    d["summary"] = json.loads(d.get("summary") or "{}")
    return d
