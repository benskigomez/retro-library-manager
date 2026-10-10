"""Downloading games: single files, multi-file zips, CHD conversion."""
import io
import os
import sys
import time
import zipfile
from pathlib import Path

from app import download
from tests.conftest import run_job

FAKE = Path(__file__).with_name("fake_chdman.py")


def test_single_file_download(client, roms):
    (roms / "gb").mkdir()
    (roms / "gb" / "Tetris (World) [!].gb").write_bytes(b"T" * 1000)
    r = client.get("/download/gb/Tetris (World) [!].gb")
    assert r.status_code == 200 and r.content == b"T" * 1000
    assert "attachment" in r.headers["content-disposition"]


def test_multitrack_cue_is_zipped(client, roms):
    d = roms / "psx"
    d.mkdir()
    (d / "Game (USA).cue").write_text('FILE "Game (USA) (Track 1).bin" BINARY\nFILE "Game (USA) (Track 2).bin" BINARY\n')
    (d / "Game (USA) (Track 1).bin").write_bytes(b"1" * 5000)
    (d / "Game (USA) (Track 2).bin").write_bytes(b"2" * 3000)
    r = client.get("/download/psx/Game (USA).cue")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "Game%20%28USA%29.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["Game (USA) (Track 1).bin", "Game (USA) (Track 2).bin", "Game (USA).cue"]
        assert zf.read("Game (USA) (Track 2).bin") == b"2" * 3000
    page = client.get("/system/psx").text
    assert "zip</a>" in page


def test_m3u_includes_all_discs(roms):
    d = roms / "psx"
    d.mkdir()
    for n in (1, 2):
        (d / f"G (Disc {n}).cue").write_text(f'FILE "G (Disc {n}).bin" BINARY\n')
        (d / f"G (Disc {n}).bin").write_bytes(b"x")
    (d / "G.m3u").write_text("G (Disc 1).cue\nG (Disc 2).cue\n")
    names = sorted(p.name for p in download.game_files(d / "G.m3u"))
    assert names == ["G (Disc 1).bin", "G (Disc 1).cue", "G (Disc 2).bin", "G (Disc 2).cue", "G.m3u"]


def test_path_traversal_refused(client, roms):
    (roms / "gb").mkdir()
    (roms / "secret.txt").write_text("no")
    assert client.get("/download/gb/..%2Fsecret.txt").status_code == 404
    assert client.get("/download/gb/missing.gb").status_code == 404


def test_chd_convert_job_and_prepared_download(client, roms):
    state = client.app.state.rlm
    cmd = f"{Path(sys.executable).as_posix()} {FAKE.as_posix()}"
    state.settings.update_config(lambda d: d["tools"].__setitem__("chdman", cmd))
    state.reload()
    (roms / "psx").mkdir()
    (roms / "psx" / "Game (USA).chd").write_bytes(b"MComprHD")
    r = client.post("/download/psx/Game (USA).chd/convert", data={"format": "cue"})
    assert r.status_code in (200, 303)
    job = state.queue.recent(1)[0]
    for _ in range(100):
        job = state.queue.get(job["id"])
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    assert "Download CUE" in client.get(f"/jobs/{job['id']}").text
    r = client.get(f"/download/prepared/{job['id']}")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["Game (USA).bin", "Game (USA).cue"]
    # the library itself was not touched
    assert sorted(p.name for p in (roms / "psx").iterdir()) == ["Game (USA).chd"]


def test_dvd_iso_and_cleanup(state, roms):
    cmd = f"{Path(sys.executable).as_posix()} {FAKE.as_posix()}"
    state.settings.update_config(lambda d: d["tools"].__setitem__("chdman", cmd))
    state.reload()
    (roms / "ps2").mkdir()
    (roms / "ps2" / "Big (USA).chd").write_bytes(b"data")
    job_id = state.queue.submit("download_convert", "t", {"system": "ps2", "name": "Big (USA).chd", "format": "iso"})
    job = run_job(state, job_id)
    files = download.prepared_files(state.cfg, job_id)
    assert job["status"] == "done" and [f.name for f in files] == ["Big (USA).iso"]
    old = time.time() - download.PREPARED_TTL - 10
    os.utime(files[0].parent, (old, old))
    assert download.cleanup_prepared(state.cfg) == 1 and not files[0].exists()


def test_convert_rejects_non_chd(client, roms):
    (roms / "psx").mkdir()
    (roms / "psx" / "x.cue").write_text("")
    assert client.post("/download/psx/x.cue/convert", data={"format": "cue"}).status_code == 400
