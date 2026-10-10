"""Integration tests for the extraction engine with synthetic archives."""
import os
import shutil
import struct
import zipfile

import pytest

from app import archives, extract, fsutil
from app.hooks import lynx
from tests.conftest import make_zip, run_job, sevenz_bytes, zip_bytes

ROM = b"\x00" * 1024


def submit(state, archive, system, import_mode="none"):
    job_id = state.queue.submit("extract", "t", {"archive": str(archive), "system": system,
                                                  "import_mode": import_mode}, system=system)
    return run_job(state, job_id)


def test_flat_archive(state, roms):
    src = make_zip(roms / "_source" / "gb-roms.zip", {
        "Tetris (World) (Rev 1).gb": ROM,
        "10-Pin Bowling (USA) (Proto).gb": ROM,
        "Some Game (Europe).gb": ROM,
        "readme.txt": b"hi",
    })
    job = submit(state, src, "gb")
    assert job["status"] == "done", job
    assert sorted(p.name for p in (roms / "gb").iterdir()) == ["Tetris (World) (Rev 1).gb"]
    assert job["summary"]["added"] == 1 and job["summary"]["excluded"] == 2
    assert job["summary"]["skipped"] == 1  # readme.txt


def test_subfolder_entries_extract_flat_and_bios_staged(state, roms):
    src = make_zip(roms / "_source" / "gb-roms.zip", {
        "GB/Tetris (World).gb": ROM,
        "GB/Deep/Pokemon - Yellow Version (USA, Europe) (CGB+SGB Enhanced).gb": ROM,
        "GB/[BIOS] Nintendo Game Boy Boot ROM (World) (Rev 1).gb": b"boot",
    })
    job = submit(state, src, "gb")
    assert job["status"] == "done"
    assert {p.name for p in (roms / "gb").iterdir()} == {
        "Tetris (World).gb", "Pokemon - Yellow Version (USA, Europe) (CGB+SGB Enhanced).gb"}
    assert (roms / "_bios" / "gb" / "[BIOS] Nintendo Game Boy Boot ROM (World) (Rev 1).gb").read_bytes() == b"boot"
    assert job["summary"]["bios_staged"] == 1


def test_enhancement_chips_are_staged_not_extracted(state, roms):
    src = make_zip(roms / "_source" / "snes-roms.zip", {
        "Pilotwings (USA).sfc": ROM,
        "DSP1 (World) (Enhancement Chip).bin": b"d" * 8192,
        "Super Game Boy SGB-CPU (World) (Enhancement Chip).bin": b"s" * 256,
    })
    job = submit(state, src, "snes")
    assert [p.name for p in (roms / "snes").iterdir()] == ["Pilotwings (USA).sfc"]
    assert sorted(p.name for p in (roms / "_bios" / "snes").iterdir()) == [
        "DSP1 (World) (Enhancement Chip).bin", "Super Game Boy SGB-CPU (World) (Enhancement Chip).bin"]
    assert job["summary"]["bios_staged"] == 2


def test_enhancement_chip_inner_zips_are_unpacked_into_bios(state, roms):
    src = make_zip(roms / "_source" / "snes-roms.zip", {
        "SNES/CX4 (World) (Enhancement Chip).zip": zip_bytes({"CX4 (World) (Enhancement Chip).bin": b"c" * 3072}),
        "SNES/[BIOS] Super NES CD-ROM - Boot ROM (Japan) (En) (v0.95) (Proto).zip":
            zip_bytes({"[BIOS] Super NES CD-ROM - Boot ROM (Japan) (En) (v0.95) (Proto).sfc": b"b" * 512}),
        "SNES/Pilotwings (USA).zip": zip_bytes({"Pilotwings (USA).sfc": ROM}),
    })
    p = extract.plan(state.cfg, state.system("snes"), src)
    assert p.totals == {"bios": 2, "include": 1}
    job = submit(state, src, "snes")
    assert job["summary"]["bios_staged"] == 2
    assert sorted(f.name for f in (roms / "_bios" / "snes").iterdir()) == [
        "CX4 (World) (Enhancement Chip).bin", "[BIOS] Super NES CD-ROM - Boot ROM (Japan) (En) (v0.95) (Proto).sfc"]
    assert [f.name for f in (roms / "snes").iterdir()] == ["Pilotwings (USA).sfc"]
    # staged by stem: the next plan reports them present instead of staging zips again
    assert extract.plan(state.cfg, state.system("snes"), src).totals == {"present": 3}


def test_inner_zip_and_7z_per_game(state, roms, tmp_path):
    src = make_zip(roms / "_source" / "snes-roms.zip", {
        "Super Game (USA).zip": zip_bytes({"Super Game (USA).sfc": b"S" * 512, "Super Game (USA).nfo": b"x"}),
        "Other Game (USA).7z": sevenz_bytes({"Other Game (USA).sfc": b"O" * 512}, tmp_path),
        "Euro Game (Europe).zip": zip_bytes({"Euro Game (Europe).sfc": b"E"}),
    })
    job = submit(state, src, "snes")
    assert job["status"] == "done", job
    assert sorted(p.name for p in (roms / "snes").iterdir()) == ["Other Game (USA).sfc", "Super Game (USA).sfc"]
    assert (roms / "snes" / "Other Game (USA).sfc").read_bytes() == b"O" * 512
    # temp copies cleaned up
    assert not any((tmp_path / "tmp").glob("job-*"))


def test_outer_7z(state, roms, tmp_path):
    data = sevenz_bytes({"NES/Zelda (USA).nes": b"Z" * 64, "NES/Bad (Japan).nes": b"B"}, tmp_path)
    src = roms / "_source" / "nes-roms.7z"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    job = submit(state, src, "nes")
    assert job["status"] == "done", job
    assert [p.name for p in (roms / "nes").iterdir()] == ["Zelda (USA).nes"]


def test_rerun_is_idempotent_and_dedupes_by_stem(state, settings, roms):
    src = make_zip(roms / "_source" / "gba-roms.zip", {
        "A (USA).gba": ROM, "B (USA).zip": zip_bytes({"B (USA).gba": ROM})})
    (roms / "gba").mkdir()
    (roms / "gba" / "A (USA).sav").write_bytes(b"save")  # same stem -> already present
    first = submit(state, src, "gba")
    assert first["summary"]["added"] == 1 and first["summary"]["present"] == 1
    p = extract.plan(settings.config, state.system("gba"), src)
    assert p.totals == {"present": 2}
    second = submit(state, src, "gba")
    assert second["summary"].get("added", 0) == 0 and second["summary"]["present"] == 2
    assert (roms / "gba" / "A (USA).sav").read_bytes() == b"save"


def submit_opts(state, archive, system, **extra):
    params = {"archive": str(archive), "system": system, "import_mode": "none"} | extra
    return run_job(state, state.queue.submit("extract", "t", params, system=system))


CUE2 = b'FILE "Game (USA) (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\nFILE "Game (USA) (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n'


def test_disc_set_copied_whole_when_copying_originals(state, roms):
    src = make_zip(roms / "psx.zip", {"PSX/Game (USA).cue": CUE2, "PSX/Game (USA) (Track 1).bin": b"\0" * 2352,
                                      "PSX/Game (USA) (Track 2).bin": b"\1" * 2352})
    p = extract.plan(state.cfg, state.system("psx"), src)
    assert p.totals == {"include": 1, "part": 2}  # one game, its tracks travel with it
    job = submit_opts(state, src, "psx", convert=False)
    assert job["summary"]["added"] == 1, state.queue.items(job["id"])
    assert {f.name for f in (roms / "psx").iterdir()} == {
        "Game (USA).cue", "Game (USA) (Track 1).bin", "Game (USA) (Track 2).bin"}


def test_selecting_a_cue_brings_its_tracks(state, roms):
    src = make_zip(roms / "psx.zip", {"Game (USA).cue": CUE2, "Game (USA) (Track 1).bin": b"\0",
                                      "Game (USA) (Track 2).bin": b"\1"})
    p = extract.plan(state.cfg, state.system("psx"), src, select={"Game (USA).cue"})
    assert sorted(r.entry for r in p.rows) == ["Game (USA) (Track 1).bin", "Game (USA) (Track 2).bin", "Game (USA).cue"]
    # ...and ticking only a track brings the sheet and the other track
    p = extract.plan(state.cfg, state.system("psx"), src, select={"Game (USA) (Track 2).bin"})
    assert len(p.rows) == 3


def test_incomplete_or_mixed_disc_sets_are_refused(state, roms):
    src = make_zip(roms / "psx.zip", {"Game (USA).cue": CUE2, "Game (USA) (Track 1).bin": b"\0"})
    p = extract.plan(state.cfg, state.system("psx"), src)
    assert p.rows[0].decision == "skipped" and "Track 2" in p.rows[0].rule
    (roms / "psx").mkdir()
    (roms / "psx" / "Game (USA) (Track 2).bin").write_bytes(b"old")
    src2 = make_zip(roms / "psx2.zip", {"Game (USA).cue": CUE2, "Game (USA) (Track 1).bin": b"\0",
                                        "Game (USA) (Track 2).bin": b"\1"})
    p = extract.plan(state.cfg, state.system("psx"), src2)
    assert p.rows[0].decision == "skipped" and "not mixing" in p.rows[0].rule
    job = submit_opts(state, src2, "psx", convert=False)
    assert job["summary"].get("added", 0) == 0
    assert sorted(f.name for f in (roms / "psx").iterdir()) == ["Game (USA) (Track 2).bin"]


def test_clonecd_layout_is_not_partially_copied(state, roms):
    src = make_zip(roms / "psx.zip", {"Clone (USA).ccd": b"[CloneCD]", "Clone (USA).img": b"\0" * 2352,
                                      "Clone (USA).sub": b"\0"})
    p = extract.plan(state.cfg, state.system("psx"), src)
    assert {r.decision for r in p.rows} == {"skipped"}
    assert any("CloneCD" in r.rule for r in p.rows)
    submit_opts(state, src, "psx", convert=False)
    assert not (roms / "psx").exists() or not any((roms / "psx").iterdir())


def test_inner_disc_zip_copied_whole_or_refused(state, roms):
    src = make_zip(roms / "psx.zip", {
        "Good (USA).zip": zip_bytes({"Good (USA).cue": CUE2.replace(b"Game", b"Good"),
                                     "Good (USA) (Track 1).bin": b"\0", "Good (USA) (Track 2).bin": b"\1",
                                     "readme.txt": b"x"}),
        "Clone (USA).zip": zip_bytes({"Clone (USA).ccd": b"x", "Clone (USA).img": b"\0", "Clone (USA).sub": b"\0"}),
    })
    job = submit_opts(state, src, "psx", convert=False)
    assert sorted(f.name for f in (roms / "psx").iterdir()) == [
        "Good (USA) (Track 1).bin", "Good (USA) (Track 2).bin", "Good (USA).cue"]
    items = {i["name"]: i for i in state.queue.items(job["id"])}
    assert items["Clone (USA).zip"]["status"] == "skipped" and ".ccd" in items["Clone (USA).zip"]["detail"]


def test_folder_as_source(state, roms, tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "sub" / "Tetris (World).gb").write_bytes(ROM)
    (inbox / "Euro (Europe).gb").write_bytes(ROM)
    job = submit(state, inbox, "gb")
    assert job["status"] == "done", job
    assert [f.name for f in (roms / "gb").iterdir()] == ["Tetris (World).gb"]
    assert (inbox / "sub" / "Tetris (World).gb").exists()  # originals left in place


def test_never_overwrites(state, roms):
    (roms / "gb").mkdir()
    (roms / "gb" / "Tetris (World).gb").write_bytes(b"original")
    src = make_zip(roms / "src.zip", {"Tetris (World).gb": b"new"})
    submit(state, src, "gb")
    assert (roms / "gb" / "Tetris (World).gb").read_bytes() == b"original"


@pytest.mark.parametrize("backend", ["python", "unzip"])
def test_glob_metacharacters_in_names(state, roms, backend):
    if backend == "unzip" and not shutil.which("unzip"):
        pytest.skip("unzip not installed")
    names = ["Army Men - Sarge_s Heroes 2 (U) [!].z64", "Bracket [a] (U) [!].z64", "Question (USA) [T+Eng].z64"]
    if os.name != "nt":  # Windows file names cannot contain * or ?
        names += ["Star*Game (USA) [b1].z64", "What? (USA).z64"]
    src = make_zip(roms / "n64.zip", {n: n.encode() * 10 for n in names})
    archives.configure_tools("7zz", "unzip", backend)
    try:
        job = submit(state, src, "n64")
    finally:
        archives.configure_tools("7zz", "unzip", "python")
    assert job["status"] == "done"
    assert job["summary"].get("failed", 0) == 0, state.queue.items(job["id"])
    for n in names:
        assert (roms / "n64" / n).read_bytes() == n.encode() * 10


def test_escape_unzip_pattern():
    assert fsutil.escape_unzip_pattern("A [!] *?.z64") == "A \\[!\\] \\*\\?.z64"


def test_arcade_sets_copied_as_is(state, roms):
    inner = zip_bytes({"rom1.bin": b"1", "rom2.bin": b"2"})
    src = make_zip(roms / "_source" / "mame-roms.zip", {
        "mame/spyhunt.zip": inner, "mame/midssio.zip": inner, "mame/readme.txt": b"x", "mame/odd.dat": b"x"})
    job = submit(state, src, "mame")
    assert job["status"] == "done"
    assert sorted(p.name for p in (roms / "mame").iterdir()) == ["midssio.zip", "spyhunt.zip"]
    assert (roms / "mame" / "spyhunt.zip").read_bytes() == inner  # byte-identical, not unpacked


def test_lynx_header_writer(state, roms):
    rom = bytes(range(256)) * 2048  # 512 KiB
    src = make_zip(roms / "_source" / "lynx-roms.zip", {
        "Klax (USA, Europe).lyx": rom, "[BIOS] Atari Lynx (World).lyx": b"\x01" * 512})
    state.settings.update_config(lambda d: d["lynx"].__setitem__("rotation", {"Klax": "left"}))
    state.reload()
    job = submit(state, src, "lynx")
    assert job["status"] == "done", job
    lyx = roms / "lynx" / "Klax (USA, Europe).lyx"
    lnx = roms / "lynx" / "Klax (USA, Europe).lnx"
    assert lyx.read_bytes() == rom  # .lyx kept
    data = lnx.read_bytes()
    assert len(data) == 64 + len(rom)
    magic, bank0, bank1, ver, name, manu, rot, spare = struct.unpack("<4sHHH32s16sB5s", data[:64])
    assert magic == b"LYNX" and bank0 == len(rom) // 256 == 2048 and bank1 == 0 and ver == 1
    assert name.rstrip(b"\0") == b"Klax" and manu == b"\0" * 16 and rot == 1 and spare == b"\0" * 5
    assert data[64:] == rom
    assert (roms / "_bios" / "lynx" / "[BIOS] Atari Lynx (World).lyx").exists()
    # re-run: .lyx present, nothing new, .lnx not rewritten
    again = submit(state, src, "lynx")
    assert again["summary"].get("added", 0) == 0 and again["summary"].get("lnx_created", 0) == 0


def test_lynx_regenerate_moves_old_to_trash(state, roms):
    (roms / "lynx").mkdir()
    (roms / "lynx" / "Lexis (USA).lyx").write_bytes(b"\0" * 1024)
    lynx.write_lnx(roms / "lynx" / "Lexis (USA).lyx", roms / "lynx" / "Lexis (USA).lnx")
    assert lynx.read_rotation(roms / "lynx" / "Lexis (USA).lnx") == "none"
    state.settings.update_config(lambda d: d["lynx"].__setitem__("rotation", {"Lexis": "right"}))
    state.reload()
    job_id = state.queue.submit("lynx_regen", "t", {"stems": ["Lexis (USA)"]}, system="lynx")
    assert run_job(state, job_id)["status"] == "done"
    assert lynx.read_rotation(roms / "lynx" / "Lexis (USA).lnx") == "right"
    assert len(list((roms / "_trash").rglob("Lexis (USA).lnx"))) == 1


def test_lynx_header_rejects_bad_sizes():
    with pytest.raises(ValueError):
        lynx.make_header(1000, "x")
    assert lynx.make_header(256 * 4, "x")[4:6] == b"\x04\x00"
    assert lynx.rotation_for("Gauntlet - The Third Encounter (USA)", {"gauntlet": "right"}) == "right"


def test_import_copy_into_source(state, roms):
    src = make_zip(roms / "incoming" / "random-name.zip", {"Tetris (World).gb": ROM})
    job = submit(state, src, "gb", import_mode="copy")
    assert job["status"] == "done", job
    assert (roms / "_source" / "gb-roms.zip").exists() and src.exists()
    # a different archive with the same target name is refused, not overwritten
    other = make_zip(roms / "incoming" / "other.zip", {"Other (USA).gb": ROM})
    job = submit(state, other, "gb", import_mode="copy")
    assert job["status"] == "failed" and "already exists" in job["error"]
    with zipfile.ZipFile(roms / "_source" / "gb-roms.zip") as zf:
        assert zf.namelist() == ["Tetris (World).gb"]


def test_import_move(state, roms):
    src = make_zip(roms / "incoming" / "x.zip", {"Tetris (World).gb": ROM})
    job = submit(state, src, "gb", import_mode="move")
    assert job["status"] == "done"
    assert not src.exists() and (roms / "_source" / "gb-roms.zip").exists()


def test_unstable_archive_is_refused(roms):
    src = make_zip(roms / "growing.zip", {"a.gb": ROM})

    def sleeper(_):
        with open(src, "ab") as f:
            f.write(b"more")

    r = fsutil.wait_stable(src, 1, sleep=sleeper)
    assert not r.ok and "still changing" in r.reason


def test_truncated_zip_is_refused(roms):
    src = make_zip(roms / "trunc.zip", {"a.gb": ROM * 10})
    data = src.read_bytes()
    src.write_bytes(data[: len(data) // 2])  # same situation as a half-copied file
    r = fsutil.wait_stable(src, 0)
    assert not r.ok and "central directory" in r.reason


def test_plan_view_and_csv(client, roms):
    make_zip(roms / "_source" / "gb-roms.zip", {"Tetris (World).gb": ROM, "X (Europe).gb": ROM})
    path = str(roms / "_source" / "gb-roms.zip")
    r = client.post("/import/inspect", data={"path": path})
    assert r.status_code == 200 and 'value="gb" selected' in r.text
    r = client.post("/extract/plan", data={"path": path, "system": "gb"})
    assert "include 1" in r.text and "exclude 1" in r.text
    csv = client.get("/extract/plan.csv", params={"path": path, "system": "gb"}).text
    assert "Tetris (World).gb" in csv and "region (World)" in csv
    assert client.post("/import/inspect", data={"path": "/etc/passwd"}).status_code == 403
