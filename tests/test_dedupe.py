"""Content-based duplicate detection (CRC32 + size), independent of file names."""
import sys
from pathlib import Path

import pytest

from app import extract
from app.dedupe import ContentIndex, crc32_file
from tests.conftest import make_zip, run_job, zip_bytes
from tests.test_chd import FAKE, cue

SONIC = b"SEGA" * 4096
OTHER = b"OTHER" * 4096


def submit(state, archive, system, **extra):
    params = {"archive": str(archive), "system": system, "import_mode": "none"} | extra
    return run_job(state, state.queue.submit("extract", "t", params, system=system))


def test_same_rom_under_another_name_is_present_in_dry_run_and_skipped(state, roms):
    (roms / "megadrive").mkdir()
    (roms / "megadrive" / "Sonic the Hedgehog (U) [!].bin").write_bytes(SONIC)  # GoodTools name
    src = make_zip(roms / "set.zip", {"Sonic the Hedgehog (USA, Europe).md": SONIC, "Other Game (USA).md": OTHER})
    p = extract.plan(state.cfg, state.system("megadrive"), src, content=state.content_index("megadrive"))
    rows = {r.base: r for r in p.rows}
    sonic = rows["Sonic the Hedgehog (USA, Europe).md"]
    assert sonic.decision == "present" and "Sonic the Hedgehog (U) [!].bin" in sonic.rule
    assert rows["Other Game (USA).md"].decision == "include"
    job = submit(state, src, "megadrive")
    assert job["summary"]["added"] == 1
    assert sorted(f.name for f in (roms / "megadrive").iterdir()) == [
        "Other Game (USA).md", "Sonic the Hedgehog (U) [!].bin"]


def test_identical_content_twice_in_one_archive(state, roms):
    src = make_zip(roms / "set.zip", {"Game (USA).md": SONIC, "Game (USA, Europe).md": SONIC})
    p = extract.plan(state.cfg, state.system("megadrive"), src, content=state.content_index("megadrive"))
    assert sorted(r.decision for r in p.rows) == ["include", "skipped"]
    assert any("same content as" in r.rule for r in p.rows)
    submit(state, src, "megadrive")
    assert len(list((roms / "megadrive").iterdir())) == 1


def test_per_game_zips_checked_during_the_run(state, roms):
    (roms / "snes").mkdir()
    (roms / "snes" / "Chrono Trigger (U) [!].smc").write_bytes(SONIC)
    src = make_zip(roms / "snes-roms.zip", {
        "Chrono Trigger (USA).zip": zip_bytes({"Chrono Trigger (USA).sfc": SONIC}),
        "Super Metroid (USA).zip": zip_bytes({"Super Metroid (USA).sfc": OTHER}),
    })
    job = submit(state, src, "snes")
    assert job["summary"]["added"] == 1 and job["summary"]["present"] >= 1
    items = {i["name"]: i for i in state.queue.items(job["id"])}
    assert "Chrono Trigger (U) [!].smc" in items["Chrono Trigger (USA).sfc"]["detail"]
    assert not (roms / "snes" / "Chrono Trigger (USA).sfc").exists()


def test_disc_set_with_tracks_already_in_library(state, roms):
    (roms / "psx").mkdir()
    (roms / "psx" / "Old Name (Track 1).bin").write_bytes(SONIC)
    (roms / "psx" / "Old Name (Track 2).bin").write_bytes(OTHER)
    sheet = b'FILE "New (USA) (Track 1).bin" BINARY\nFILE "New (USA) (Track 2).bin" BINARY\n'
    src = make_zip(roms / "psx.zip", {"New (USA).cue": sheet, "New (USA) (Track 1).bin": SONIC,
                                      "New (USA) (Track 2).bin": OTHER})
    p = extract.plan(state.cfg, state.system("psx"), src, content=state.content_index("psx"))
    assert p.rows[0].decision == "present" and "same tracks" in p.rows[0].rule


def test_folder_source_without_stored_crcs(state, roms, tmp_path):
    (roms / "gba").mkdir()
    (roms / "gba" / "Mario (U).gba").write_bytes(SONIC)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "Super Mario Advance (USA).gba").write_bytes(SONIC)
    p = extract.plan(state.cfg, state.system("gba"), inbox, content=state.content_index("gba"))
    assert p.rows[0].decision == "present"


def test_converting_a_disc_already_in_library_is_skipped(state, roms):
    cmd = f"{Path(sys.executable).as_posix()} {FAKE.as_posix()}"
    state.settings.update_config(lambda d: d["tools"].__setitem__("chdman", cmd))
    state.reload()
    (roms / "pcenginecd").mkdir()
    (roms / "pcenginecd" / "Old (Track 1).bin").write_bytes(SONIC)
    src = make_zip(roms / "pce.zip", {"Game (USA).zip": zip_bytes({"Game (USA).cue": cue("t1.bin"), "t1.bin": SONIC})})
    job = submit(state, src, "pcenginecd")  # prefers CHD
    assert job["summary"].get("converted", 0) == 0 and job["summary"]["present"] >= 1
    assert not (roms / "pcenginecd" / "Game (USA).chd").exists()


def test_crcs_are_cached(state, roms):
    (roms / "gb").mkdir()
    f = roms / "gb" / "A.gb"
    f.write_bytes(SONIC)
    idx = ContentIndex(state.db, roms / "gb")
    assert idx.find(crc32_file(f), len(SONIC)) == "A.gb"
    assert state.db.one("SELECT crc FROM crc_cache WHERE path=?", (str(f),))["crc"] == crc32_file(f)
    # files of a different size are never hashed
    assert idx.find(123, 7) is None and state.db.one("SELECT count(*) AS n FROM crc_cache")["n"] == 1


def test_dry_run_page_shows_content_match(client, roms):
    (roms / "megadrive").mkdir()
    (roms / "megadrive" / "Sonic (U) [!].bin").write_bytes(SONIC)
    src = make_zip(roms / "incoming" / "s.zip", {"Sonic the Hedgehog (USA, Europe).md": SONIC})
    r = client.post("/extract/plan", data={"path": str(src), "system": "megadrive"})
    assert "same ROM already in library as Sonic (U) [!].bin" in r.text
