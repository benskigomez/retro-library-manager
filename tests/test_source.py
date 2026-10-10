"""Source browser: download entries, copy/convert selected entries into the library."""
import sys
from pathlib import Path

import pytest

from app import archives, extract
from tests.conftest import make_zip, run_job, sevenz_bytes, zip_bytes
from tests.test_chd import FAKE, cue

ROM = b"\x01" * 512


@pytest.fixture()
def src(roms):
    return make_zip(roms / "_source" / "snes-roms.zip", {
        "SNES/Terranigma (Europe).sfc": b"T" * 512,
        "SNES/Chrono Trigger (USA).sfc": b"C" * 512,
        "SNES/Proto Game (USA) (Proto).sfc": b"P" * 512,
    })


def test_source_pages_and_entry_download(client, roms, src):
    r = client.get("/source")
    assert r.status_code == 200 and "snes-roms.zip" in r.text
    r = client.get("/source/view", params={"path": str(src)})
    assert r.status_code == 200 and "Terranigma (Europe).sfc" in r.text and "exclude" in r.text
    r = client.get("/source/download", params={"path": str(src), "member": "SNES/Terranigma (Europe).sfc"})
    assert r.content == b"T" * 512 and "Terranigma" in r.headers["content-disposition"]
    assert client.get("/source/download", params={"path": str(src)}).content == src.read_bytes()
    # only files inside _source, only real members
    (roms / "outside.zip").write_bytes(src.read_bytes())
    assert client.get("/source/download", params={"path": str(roms / "outside.zip")}).status_code == 404
    assert client.get("/source/download", params={"path": str(src), "member": "nope"}).status_code == 404


def test_filter_links_keep_archive_path(client, src):
    import html
    import re

    page = client.get("/source/view", params={"path": str(src)}).text
    href = html.unescape(re.search(r'href="(\?path=[^"]*show=exclude)"', page).group(1))
    r = client.get("/source/view" + href)
    assert r.status_code == 200 and "Terranigma (Europe).sfc" in r.text and "Chrono Trigger" not in r.text


def test_import_page_prefills_path(client, src):
    assert f'value="{src}"' in client.get("/import", params={"path": str(src)}).text


def test_selected_entries_override_filter(state, roms, src):
    plan = extract.plan(state.cfg, state.system("snes"), src, select={"SNES/Terranigma (Europe).sfc"})
    assert [(r.base, r.decision) for r in plan.rows] == [("Terranigma (Europe).sfc", "include")]
    assert plan.rows[0].rule.startswith("selected manually")
    job = run_job(state, state.queue.submit("extract", "t", {
        "archive": str(src), "system": "snes", "import_mode": "none",
        "select": ["SNES/Terranigma (Europe).sfc", "SNES/Proto Game (USA) (Proto).sfc"]}))
    assert job["status"] == "done"
    assert sorted(p.name for p in (roms / "snes").iterdir()) == ["Proto Game (USA) (Proto).sfc", "Terranigma (Europe).sfc"]
    # source untouched; re-selecting reports present, never overwrites
    job = run_job(state, state.queue.submit("extract", "t", {
        "archive": str(src), "system": "snes", "import_mode": "none", "select": ["SNES/Terranigma (Europe).sfc"]}))
    assert job["summary"]["present"] == 1 and job["summary"].get("added", 0) == 0


def test_preview_and_run_routes(client, roms, src):
    r = client.post("/source/preview", data={"path": str(src), "system": "snes", "mode": "copy",
                                              "names": ["SNES/Terranigma (Europe).sfc"]})
    assert "1 to add" in r.text and "selected manually" in r.text
    r = client.post("/source/run", data={"path": str(src), "system": "snes", "mode": "copy",
                                          "names": ["SNES/Terranigma (Europe).sfc"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    assert client.post("/source/run", data={"path": str(src), "system": "snes", "mode": "chd",
                                             "names": ["SNES/Terranigma (Europe).sfc"]}).status_code == 400


@pytest.fixture()
def chd_state(state):
    cmd = f"{Path(sys.executable).as_posix()} {FAKE.as_posix()}"
    state.settings.update_config(lambda d: d["tools"].__setitem__("chdman", cmd))
    state.reload()
    return state


def test_convert_inner_disc_archive_to_chd_without_extra_backup(chd_state, roms):
    src = make_zip(roms / "_source" / "pcenginecd-roms.zip", {
        "Game A (USA).zip": zip_bytes({"Game A (USA).cue": cue("a.bin"), "a.bin": b"\0" * 2352}),
        "Game B (USA).zip": zip_bytes({"Game B (USA).cue": cue("b.bin"), "b.bin": b"\0" * 2352}),
    })
    job = run_job(chd_state, chd_state.queue.submit("extract", "t", {
        "archive": str(src), "system": "pcenginecd", "import_mode": "none",
        "select": ["Game A (USA).zip"], "convert": True}))
    assert job["status"] == "done", job
    assert [p.name for p in (roms / "pcenginecd").iterdir()] == ["Game A (USA).chd"]
    assert not (roms / "_source" / "pcenginecd").exists()  # no redundant backup


def test_convert_backup_file_stays_in_source(chd_state, roms):
    backup = make_zip(roms / "_source" / "pcenginecd" / "Game C (USA).zip",
                      {"Game C (USA).cue": cue("c.bin"), "c.bin": b"\0" * 2352})
    job = run_job(chd_state, chd_state.queue.submit("chd", "t", {"input": str(backup), "system": "pcenginecd", "only": []}))
    assert job["status"] == "done"
    assert (roms / "pcenginecd" / "Game C (USA).chd").exists() and backup.exists()


def test_stream_member_7z(tmp_path):
    p = tmp_path / "x.7z"
    p.write_bytes(sevenz_bytes({"dir/A [!] (USA).nes": b"A" * 3000}, tmp_path))
    assert b"".join(archives.stream_member(p, "dir/A [!] (USA).nes")) == b"A" * 3000


def test_source_pages_render(client, roms, src):
    make_zip(roms / "_source" / "pcenginecd" / "G (USA).zip", {"G (USA).cue": cue("g.bin"), "g.bin": b"\0"})
    assert client.get("/source/backups/pcenginecd").status_code == 200
    r = client.get("/source/view", params={"path": str(roms / "_source" / "pcenginecd" / "G (USA).zip")})
    assert r.status_code == 200 and "convert this archive to CHD" in r.text
    r = client.post("/source/preview", data={"path": str(roms / "_source" / "pcenginecd" / "G (USA).zip"),
                                              "system": "pcenginecd", "mode": "chd_file"})
    assert "G (USA).chd" in r.text
