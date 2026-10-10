"""Shared fixtures. All test data is synthetic: dummy bytes, generated archives."""
from __future__ import annotations

import base64
import os
import zipfile
from pathlib import Path

import pytest

from app.config import Settings


@pytest.fixture()
def roms(tmp_path: Path) -> Path:
    root = tmp_path / "roms"
    root.mkdir()
    return root


@pytest.fixture()
def settings(roms: Path, tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("RLM_PASSWORD", "test-password")
    monkeypatch.delenv("RLM_USERNAME", raising=False)
    monkeypatch.delenv("RLM_CONFIG", raising=False)
    s = Settings(roms=str(roms))
    s.update_config(lambda d: (d["stability"].__setitem__("wait_seconds", 0),
                               d["paths"].__setitem__("temp", str(tmp_path / "tmp"))))
    return s


@pytest.fixture()
def state(settings):
    from app.auth import bootstrap_password
    from app.handlers import register_handlers
    from app.state import State

    bootstrap_password(settings, log=lambda m: None)
    st = State(settings)
    register_handlers(st)
    yield st
    st.db.close()


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, start_workers=True)
    with TestClient(app) as c:
        c.auth = ("admin", "test-password")
        yield c


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def zip_bytes(members: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def sevenz_bytes(members: dict[str, bytes], tmp: Path) -> bytes:
    import py7zr

    p = tmp / f"inner-{os.getpid()}-{len(members)}.7z"
    with py7zr.SevenZipFile(p, "w") as z:
        for name, data in members.items():
            z.writestr(data, name)
    data = p.read_bytes()
    p.unlink()
    return data


def run_job(state, job_id: int) -> dict:
    """Run a queued job synchronously in the test thread."""
    job = state.queue.get(job_id)
    state.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
    state.queue.run_job(job)
    return state.queue.get(job_id)
