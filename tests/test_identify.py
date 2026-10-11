"""Identify: a mixed archive/folder of unknown systems, synthetic files with real-format headers."""
import json
import re
import time

from app.identify import identify, summarize, sniff_disc
from tests.conftest import make_zip, zip_bytes


def genesis_rom():
    data = bytearray(64 * 1024)
    data[0x100:0x110] = b"SEGA MEGA DRIVE "
    return bytes(data)


def pce_cd_track():
    data = bytearray(2352 * 4)
    data[2352 + 0x20:2352 + 0x20 + 23] = b"PC Engine CD-ROM SYSTEM"
    return bytes(data)


def saturn_iso():
    data = bytearray(64 * 1024)
    data[0:16] = b"SEGA SEGASATURN "
    return bytes(data)


CUE = (b'FILE "Disc Game (USA) (Track 1).bin" BINARY\n  TRACK 01 AUDIO\n'
       b'FILE "Disc Game (USA) (Track 2).bin" BINARY\n  TRACK 02 MODE1/2352\n')


def mixed_members():
    m = {
        "Tetris (World).gb": b"\0" * 32768,
        "GBC/Mono Game (USA).gb": b"\0" * 32768,
        "Sonic (USA).bin": genesis_rom(),
        "Pitfall (USA).a26": b"\0" * 4096,
        "Combat (USA).bin": b"\0" * 2048,
        "GBA/Advance Game (USA).zip": zip_bytes({"Advance Game (USA).gba": b"\0" * 1024, "x.txt": b""}),
        "Disc/Disc Game (USA).cue": CUE,
        "Disc/Disc Game (USA) (Track 1).bin": b"\0" * 2352,
        "Disc/Disc Game (USA) (Track 2).bin": pce_cd_track(),
        "Saturn Game (USA).iso": saturn_iso(),
        "PSP/Portable (USA).iso": b"\0" * 4096,
        "weird.xyz": b"?",
        "readme.txt": b"hi",
    }
    for i in range(7):
        m[f"SNES/Game {i} (USA).zip"] = zip_bytes({f"Game {i} (USA).sfc": b"\0" * 1024})
    return m


def test_identify_mixed_archive(state, roms):
    src = make_zip(roms / "incoming" / "mixed.zip", mixed_members())
    idents = {i.entry: i for i in identify(src, state.systems)}
    assert idents["Tetris (World).gb"].system == "gb"
    assert idents["GBC/Mono Game (USA).gb"].system == "gbc"  # folder name beats the primary-format rule
    assert idents["Sonic (USA).bin"].system == "megadrive"
    assert idents["Pitfall (USA).a26"].system == "atari2600"
    assert idents["Combat (USA).bin"].system is None and "atari2600" in idents["Combat (USA).bin"].candidates
    assert idents["GBA/Advance Game (USA).zip"].system == "gba"
    disc = idents["Disc/Disc Game (USA).cue"]
    assert disc.system == "pcenginecd" and disc.tracks == 2
    assert "Disc/Disc Game (USA) (Track 2).bin" not in idents  # tracks travel with their sheet
    assert idents["Saturn Game (USA).iso"].system == "saturn"
    assert idents["PSP/Portable (USA).iso"].system == "psp" and "folder" in idents["PSP/Portable (USA).iso"].how
    assert idents["weird.xyz"].system is None and not idents["weird.xyz"].candidates
    assert "readme.txt" not in idents
    snes = [i for e, i in idents.items() if e.startswith("SNES/")]
    assert len(snes) == 7 and {i.system for i in snes} == {"snes"}
    assert any("sampled" in i.how for i in snes)  # the rest followed the sampled siblings
    decided, ambiguous, unknown = summarize(list(idents.values()))
    assert set(decided) >= {"gb", "megadrive", "snes", "pcenginecd", "saturn", "psp"}
    assert [i.entry for i in unknown] == ["weird.xyz"]


def test_sniff_disc_signatures():
    gc = bytearray(64)
    gc[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
    assert sniff_disc(bytes(gc))[0] == "gamecube"
    ps = bytearray(40000)
    ps[0x8001:0x8006] = b"CD001"
    ps[0x8008:0x8013] = b"PLAYSTATION"
    assert sniff_disc(bytes(ps), 500 * 2**20)[1] == ["psx", "ps2"]
    assert sniff_disc(bytes(ps), 2 * 2**30)[0] == "ps2"


def test_identify_flow_end_to_end(client, roms):
    state = client.app.state.rlm
    src = make_zip(roms / "incoming" / "mixed.zip", mixed_members())
    r = client.post("/identify/scan", data={"path": str(src)})
    assert r.status_code == 200 and "Needs your choice" in r.text
    assigned = re.search(r'name="assigned" value="([^"]*)"', r.text).group(1).replace("&#34;", '"').replace("&quot;", '"')
    form = {"path": str(src), "assigned": assigned, "keep": "1"}
    for m in re.finditer(r'name="amb_members_(\d+)" value="([^"]*)"', r.text):
        form[f"amb_members_{m.group(1)}"] = m.group(2).replace("&#34;", '"').replace("&quot;", '"')
        members = json.loads(form[f"amb_members_{m.group(1)}"])
        form[f"amb_{m.group(1)}"] = "atari2600" if "Combat (USA).bin" in members else "skip"
    r = client.post("/identify/plan", data=form)
    assert r.status_code == 200 and "use_gb" in r.text and "use_atari2600" in r.text
    groups = re.search(r'name="groups" value="([^"]*)"', r.text).group(1).replace("&#34;", '"').replace("&quot;", '"')
    run = {"path": str(src), "groups": groups, "keep": "1", "use_gb": "1", "use_gbc": "1", "use_megadrive": "1",
           "use_atari2600": "1",
           "use_snes": "1", "use_pcenginecd": "1", "mode_pcenginecd": "original"}
    r = client.post("/identify/run", data=run, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(200):
        if all(j["status"] in ("done", "failed") for j in state.queue.recent(20)):
            break
        time.sleep(0.05)
    jobs = state.queue.recent(20)
    assert all(j["status"] == "done" for j in jobs), [(j["title"], j["error"]) for j in jobs]
    assert [f.name for f in (roms / "gb").iterdir()] == ["Tetris (World).gb"]
    assert [f.name for f in (roms / "megadrive").iterdir()] == ["Sonic (USA).bin"]
    assert sorted(f.name for f in (roms / "atari2600").iterdir()) == ["Combat (USA).bin", "Pitfall (USA).a26"]
    assert len(list((roms / "snes").iterdir())) == 7
    assert sorted(f.name for f in (roms / "pcenginecd").iterdir()) == [
        "Disc Game (USA) (Track 1).bin", "Disc Game (USA) (Track 2).bin", "Disc Game (USA).cue"]
    assert not (roms / "saturn").exists()  # not checked -> not run
    assert (roms / "_source" / "mixed.zip").read_bytes() == src.read_bytes()  # kept as-is
    assert src.exists()  # original left in place


def test_identify_folder_source(state, tmp_path):
    inbox = tmp_path / "downloads"
    inbox.mkdir()
    (inbox / "a.gba").write_bytes(b"\0" * 10)
    (inbox / "Sonic.bin").write_bytes(genesis_rom())
    idents = {i.entry: i.system for i in identify(inbox, state.systems)}
    assert idents == {"a.gba": "gba", "Sonic.bin": "megadrive"}


def test_folder_upload_then_identify(client, roms):
    r = client.put("/identify/upload-file", params={"batch": "20261010-ab12", "rel": "My Stuff/gba/a.gba"}, content=b"\0" * 10)
    assert r.status_code == 200
    folder = r.json()["path"]
    r = client.put("/identify/upload-file", params={"batch": "20261010-ab12", "rel": "My Stuff/Sonic.bin"},
                   content=genesis_rom())
    assert r.json()["path"] == folder
    assert (roms / "_manager" / "uploads" / "folder-20261010-ab12" / "My Stuff" / "gba" / "a.gba").exists()
    for bad in ("../escape.gb", "a/../../x.gb", ".hidden/x.gb", ""):
        assert client.put("/identify/upload-file", params={"batch": "20261010-ab12", "rel": bad}, content=b"x").status_code == 400
    assert client.put("/identify/upload-file", params={"batch": "../x", "rel": "a.gb"}, content=b"x").status_code == 400
    assert client.put("/identify/upload-file", params={"batch": "20261010-ab12", "rel": "My Stuff/Sonic.bin"},
                      content=b"other").status_code == 409  # never overwrites
    r = client.post("/identify/scan", data={"path": folder})
    assert r.status_code == 200 and "gba" in r.text and "megadrive" in r.text


def test_system_page_search(client, roms):
    (roms / "snes").mkdir()
    for n in ("Chrono Trigger (USA).sfc", "Super Metroid (USA).sfc", "Terranigma (Europe).sfc"):
        (roms / "snes" / n).write_bytes(b"\0")
    r = client.get("/system/snes", params={"q": "metroid"})
    assert "Super Metroid" in r.text and "Chrono Trigger" not in r.text
    r = client.get("/system/snes", params={"q": "europe"})
    assert "Terranigma" in r.text and "Super Metroid" not in r.text
    assert "No files match" in client.get("/system/snes", params={"q": "zzz"}).text
