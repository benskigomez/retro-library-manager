"""Disc → CHD with a fake chdman."""
import sys
from pathlib import Path

import pytest

from app import chd
from tests.conftest import make_zip, run_job

FAKE = Path(__file__).with_name("fake_chdman.py")


@pytest.fixture()
def chd_state(state):
    cmd = f"{Path(sys.executable).as_posix()} {FAKE.as_posix()}"
    state.settings.update_config(lambda d: d["tools"].__setitem__("chdman", cmd))
    state.reload()
    return state


def cue(*tracks):
    out = []
    for i, t in enumerate(tracks, 1):
        out.append(f'FILE "{t}" BINARY\n  TRACK {i:02d} MODE1/2352\n    INDEX 01 00:00:00')
    return "\n".join(out).encode()


def submit(state, inp, system, only=()):
    job_id = state.queue.submit("chd", "t", {"input": str(inp), "system": system, "only": list(only)}, system=system)
    return run_job(state, job_id)


def test_multitrack_archive_converts_and_backs_up(chd_state, roms):
    inbox = roms / "incoming"
    make_zip(inbox / "Game A (USA).zip", {
        "Game A (USA).cue": cue("Game A (USA) (Track 1).bin", "Game A (USA) (Track 2).bin"),
        "Game A (USA) (Track 1).bin": b"\0" * 4704,
        "Game A (USA) (Track 2).bin": b"\0" * 2352,
    })
    job = submit(chd_state, inbox, "pcenginecd")
    assert job["status"] == "done", job
    out = roms / "pcenginecd" / "Game A (USA).chd"
    assert out.read_bytes().startswith(b"MComprHDcreatecd:Game A (USA).cue")
    assert (roms / "_source" / "pcenginecd" / "Game A (USA).zip").exists()
    assert not (inbox / "Game A (USA).zip").exists()
    item = [i for i in chd_state.queue.items(job["id"]) if i["status"] == "added"][0]
    assert item["data"]["before"] > item["data"]["after"] and 0 < item["data"]["ratio"] < 1
    assert "2 track file(s)" in item["detail"]


def test_existing_chd_is_skipped(chd_state, roms):
    inbox = roms / "incoming"
    make_zip(inbox / "Game B (USA).zip", {"Game B (USA).cue": cue("b.bin"), "b.bin": b"\0" * 2352})
    (roms / "pcenginecd").mkdir()
    (roms / "pcenginecd" / "Game B (USA).chd").write_bytes(b"old")
    games = chd.discover(chd_state.cfg, chd_state.system("pcenginecd"), inbox)
    assert games[0].status == "present"
    job = submit(chd_state, inbox, "pcenginecd")
    assert job["summary"]["present"] == 1
    assert (roms / "pcenginecd" / "Game B (USA).chd").read_bytes() == b"old"
    assert (inbox / "Game B (USA).zip").exists()


def test_failure_keeps_temp_and_reports_stderr(chd_state, roms, tmp_path):
    inbox = roms / "incoming"
    make_zip(inbox / "Broken (USA).zip", {"FAIL Broken (USA).cue": cue("x.bin"), "x.bin": b"\0" * 2352})
    job = submit(chd_state, inbox, "pcenginecd")
    items = chd_state.queue.items(job["id"])
    failed = [i for i in items if i["status"] == "failed"]
    assert failed and "synthetic chdman failure" in failed[0]["data"]["stderr"]
    assert any((tmp_path / "tmp").rglob("x.bin"))  # temp kept for inspection
    assert (inbox / "Broken (USA).zip").exists()   # original not moved


def test_missing_track_fails_before_chdman(chd_state, roms):
    inbox = roms / "incoming"
    make_zip(inbox / "Partial (USA).zip", {"Partial (USA).cue": cue("t1.bin", "t2.bin"), "t1.bin": b"\0"})
    job = submit(chd_state, inbox, "pcenginecd")
    items = chd_state.queue.items(job["id"])
    assert any("missing track" in (i["detail"] or "") and "t2.bin" in i["detail"] for i in items)
    assert not (roms / "pcenginecd").exists() or not any((roms / "pcenginecd").iterdir())


def test_unsupported_ccd_is_flagged(chd_state, roms):
    inbox = roms / "incoming"
    make_zip(inbox / "Clone (USA).zip", {"Clone (USA).ccd": b"[CloneCD]", "Clone (USA).img": b"\0", "Clone (USA).sub": b"\0"})
    games = chd.discover(chd_state.cfg, chd_state.system("psx"), inbox)
    assert games[0].status == "unsupported" and ".ccd" in games[0].detail
    job = submit(chd_state, inbox, "psx")
    assert job["summary"]["unsupported"] == 1


def test_dvd_iso_uses_createdvd_and_loose_sets(chd_state, roms):
    inbox = roms / "incoming"
    inbox.mkdir()
    (inbox / "Portable (USA).iso").write_bytes(b"\0" * 4096)
    job = submit(chd_state, inbox, "psp")
    assert job["status"] == "done"
    assert (roms / "psp" / "Portable (USA).chd").read_bytes().startswith(b"MComprHDcreatedvd:")
    assert (inbox / "Portable (USA).iso").exists()  # loose originals stay


def test_multi_disc_archive_makes_one_chd_per_cue(chd_state, roms):
    inbox = roms / "incoming"
    make_zip(inbox / "Epic (USA).zip", {
        "Epic (USA) (Disc 1).cue": cue("d1.bin"), "d1.bin": b"\0" * 2352,
        "Epic (USA) (Disc 2).cue": cue("d2.bin"), "d2.bin": b"\0" * 2352,
    })
    games = chd.discover(chd_state.cfg, chd_state.system("psx"), inbox)
    assert games[0].targets == ["Epic (USA) (Disc 1)", "Epic (USA) (Disc 2)"]
    job = submit(chd_state, inbox, "psx")
    assert job["summary"]["converted"] == 2
    assert {p.name for p in (roms / "psx").iterdir()} == {"Epic (USA) (Disc 1).chd", "Epic (USA) (Disc 2).chd"}


def test_gdi_tracks_parsed(tmp_path):
    gdi = tmp_path / "disc.gdi"
    gdi.write_text('3\n1 0 4 2352 "track 01.bin" 0\n2 600 0 2352 track02.raw 0\n3 45000 4 2352 track03.bin 0\n')
    (tmp_path / "track 01.bin").write_bytes(b"1")
    (tmp_path / "TRACK02.RAW").write_bytes(b"2")
    present, missing = chd.resolve_tracks(gdi)
    assert len(present) == 2 and missing == ["track03.bin"]


def test_chd_plan_route(client, roms):
    inbox = roms / "incoming"
    make_zip(inbox / "G (USA).zip", {"G (USA).cue": cue("g.bin"), "g.bin": b"\0"})
    r = client.post("/chd/plan", data={"path": str(inbox), "system": "segacd"})
    assert r.status_code == 200 and "convert 1" in r.text and "G (USA).chd" in r.text
    assert client.post("/chd/run", data={"path": str(inbox), "system": "segacd"}).status_code == 400
