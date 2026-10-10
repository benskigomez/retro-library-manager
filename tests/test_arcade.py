"""Arcade tools with a synthetic -listxml and synthetic set zips (CRC32 of dummy bytes)."""
import xml.etree.ElementTree as ET
import zlib

import pytest

from app import gamelist
from app.arcade.mamedb import Have, Mame, import_listxml, open_db, verify_set
from tests.conftest import make_zip, run_job, zip_bytes

DATA = {k: k.encode() * 7 for k in [
    "spyhunt.1", "spyhunt.2", "ssio.snd", "csd.u7", "neo.sp1", "neo.alt", "neo.lo", "mslug.p1", "mslugx.p1", "pin.u1"]}


def crc(k):
    return f"{zlib.crc32(DATA[k]):08x}"


def listxml():
    return f"""<?xml version="1.0"?>
<mame build="0.289 (synthetic)">
  <machine name="spyhunt" sourcefile="midway/mcr3.cpp">
    <description>Spy Hunter</description><year>1983</year><manufacturer>Bally Midway</manufacturer>
    <rom name="spyhunt.1" size="70" crc="{crc('spyhunt.1')}"/>
    <rom name="spyhunt.2" size="70" crc="{crc('spyhunt.2')}"/>
    <rom name="nodump.x" size="70" status="nodump"/>
    <device_ref name="z80"/><device_ref name="midssio"/><device_ref name="midcsd"/>
  </machine>
  <machine name="midssio" isdevice="yes" runnable="no"><description>Midway SSIO</description>
    <rom name="ssio.snd" size="56" crc="{crc('ssio.snd')}"/></machine>
  <machine name="midcsd" isdevice="yes" runnable="no"><description>Cheap Squeak Deluxe</description>
    <rom name="csd.u7" size="42" crc="{crc('csd.u7')}"/></machine>
  <machine name="z80" isdevice="yes" runnable="no"><description>Zilog Z80</description></machine>
  <machine name="spyhuntr" ismechanical="yes"><description>Spy Hunter (Bally pinball)</description>
    <rom name="pin.u1" size="42" crc="{crc('pin.u1')}"/></machine>
  <machine name="neogeo" isbios="yes"><description>Neo-Geo MV-6F</description>
    <biosset name="euro" description="Europe" default="yes"/><biosset name="asia" description="Asia"/>
    <rom name="sp-s2.sp1" bios="euro" size="49" crc="{crc('neo.sp1')}"/>
    <rom name="asia-s3.rom" bios="asia" size="49" crc="{crc('neo.alt')}"/>
    <rom name="000-lo.lo" size="35" crc="{crc('neo.lo')}"/></machine>
  <machine name="mslug" romof="neogeo"><description>Metal Slug</description>
    <biosset name="euro" description="Europe" default="yes"/>
    <rom name="sp-s2.sp1" merge="sp-s2.sp1" bios="euro" size="49" crc="{crc('neo.sp1')}"/>
    <rom name="asia-s3.rom" merge="asia-s3.rom" bios="asia" size="49" crc="{crc('neo.alt')}"/>
    <rom name="000-lo.lo" merge="000-lo.lo" size="35" crc="{crc('neo.lo')}"/>
    <rom name="201-p1.p1" size="56" crc="{crc('mslug.p1')}"/></machine>
  <machine name="mslugx" cloneof="mslug" romof="mslug"><description>Metal Slug X (clone for test)</description>
    <rom name="201-p1.p1" merge="201-p1.p1" size="56" crc="{crc('mslug.p1')}"/>
    <rom name="250-p1.p1" size="63" crc="{crc('mslugx.p1')}"/>
    <rom name="000-lo.lo" merge="000-lo.lo" size="35" crc="{crc('neo.lo')}"/></machine>
  <machine name="hdgame"><description>Has Disk</description><disk name="hd" sha1="00"/></machine>
</mame>
"""


@pytest.fixture()
def mame(tmp_path):
    xml = tmp_path / "mame0289.xml"
    xml.write_text(listxml())
    db = open_db(tmp_path / "mame.db")
    result = import_listxml(db, xml)
    assert result["machines"] == 9 and result["build"] == "0.289 (synthetic)"
    yield Mame(db)
    db.close()


def z(folder, name, *keys):
    return make_zip(folder / f"{name}.zip", {k: DATA[k] for k in keys})


def test_device_requirements_spy_hunter(mame, roms):
    folder = roms / "mame"
    z(folder, "spyhunt", "spyhunt.1", "spyhunt.2")
    res = verify_set(mame, Have(folder), "spyhunt")
    assert res.status == "missing"
    assert {(m["rom"], m["supplier"]) for m in res.missing} == {("ssio.snd", "midssio"), ("csd.u7", "midcsd")}
    assert any("no good dump" in n for n in res.notes)  # nodump is informational, not a failure
    z(folder, "midssio", "ssio.snd")
    z(folder, "midcsd", "csd.u7")
    assert verify_set(mame, Have(folder), "spyhunt").status == "good"


def test_parent_and_bios_chain_and_default_biosset(mame, roms):
    folder = roms / "mame"
    z(folder, "mslug", "mslug.p1")
    z(folder, "mslugx", "mslugx.p1")
    res = verify_set(mame, Have(folder), "mslugx")
    # BIOS ROMs come from neogeo.zip; the non-default "asia" BIOS ROM is not required
    assert {(m["rom"], m["supplier"], m["supplier_present"]) for m in res.missing} == {
        ("000-lo.lo", "neogeo", False)}
    z(folder, "neogeo", "neo.sp1", "neo.lo")
    have = Have(folder)
    assert verify_set(mame, have, "mslugx").status == "good"
    assert verify_set(mame, have, "mslug").status == "good"
    assert verify_set(mame, have, "neogeo").status == "good"


def test_classification(mame):
    assert mame.classify("spyhunt") == "game"
    assert mame.classify("spyhuntr") == "mechanical"  # Bally pinball, not the arcade game
    assert mame.classify("neogeo") == "bios"
    assert mame.classify("midssio") == "device"
    assert mame.classify("mslugx") == "clone"
    assert mame.classify("notreal") == "unknown"
    assert mame.bios_of("mslugx") == "neogeo"


def test_unknown_and_disk_notes(mame, roms):
    folder = roms / "mame"
    make_zip(folder / "homebrew.zip", {"a.bin": b"a"})
    make_zip(folder / "hdgame.zip", {})
    have = Have(folder)
    assert verify_set(mame, have, "homebrew").status == "unknown"
    assert any("CHD" in n for n in verify_set(mame, have, "hdgame").notes)


def test_lookup_search(mame):
    names = [r["name"] for r in mame.search("Spy Hunter")]
    assert "spyhunt" in names and "midssio" not in names


# ---- gamelist hiding ---------------------------------------------------------------

GAMELIST = """<?xml version="1.0"?>
<gameList>
\t<!-- scraped -->
\t<game id="1"><path>./spyhunt.zip</path><name>Spy Hunter</name><rating>0.8</rating></game>
\t<game><path>./neogeo.zip</path><name>Neo Geo</name></game>
\t<game><path>midssio.zip</path><name>SSIO</name><hidden>false</hidden></game>
\t<game><path>./spyhuntr.zip</path><name>Pinball</name><hidden>true</hidden></game>
</gameList>
"""


def test_gamelist_hide(tmp_path):
    gl = tmp_path / "gamelist.xml"
    gl.write_text(GAMELIST)
    names = {"neogeo.zip": "Neo-Geo", "midssio.zip": "Midway SSIO", "midcsd.zip": "Cheap Squeak Deluxe",
             "spyhuntr.zip": "Pinball"}
    plan = {c.name: c.action for c in gamelist.plan_hide(gl, names)}
    assert plan == {"neogeo.zip": "update", "midssio.zip": "update", "midcsd.zip": "add", "spyhuntr.zip": "already"}
    bak, changes = gamelist.apply_hide(gl, names)
    assert bak and bak.read_text() == GAMELIST  # backup is the untouched original
    root = ET.parse(gl).getroot()
    games = {g.findtext("path"): g for g in root.findall("game")}
    assert games["./neogeo.zip"].findtext("hidden") == "true"
    assert games["midssio.zip"].findtext("hidden") == "true"
    assert games["./midcsd.zip"].findtext("hidden") == "true"
    assert games["./midcsd.zip"].findtext("name") == "Cheap Squeak Deluxe"
    # real game untouched
    spy = games["./spyhunt.zip"]
    assert spy.find("hidden") is None and spy.findtext("rating") == "0.8" and spy.get("id") == "1"
    assert "<!-- scraped -->" in gl.read_text()
    assert len(root.findall("game")) == 5
    # second run: nothing to do, a second distinct backup
    bak2, changes2 = gamelist.apply_hide(gl, names)
    assert {c.action for c in changes2} == {"already"} and bak2 != bak


def test_gamelist_created_when_missing(tmp_path):
    gl = tmp_path / "gamelist.xml"
    bak, changes = gamelist.apply_hide(gl, {"neogeo.zip": "Neo-Geo"})
    assert bak is None and ET.parse(gl).getroot().find("game/hidden").text == "true"


# ---- jobs through the app --------------------------------------------------------------

def test_arcade_jobs_end_to_end(state, roms, tmp_path):
    xml = tmp_path / "mame0289.xml"
    xml.write_text(listxml())
    job = run_job(state, state.queue.submit("mame_import", "t", {"xml": str(xml)}))
    assert job["status"] == "done" and job["summary"]["machines"] == 9
    folder = roms / "mame"
    z(folder, "spyhunt", "spyhunt.1", "spyhunt.2")
    z(folder, "spyhuntr", "pin.u1")
    z(folder, "midssio", "ssio.snd")
    job = run_job(state, state.queue.submit("mame_verify", "t", {"system": "mame"}))
    assert job["summary"]["good"] == 2 and job["summary"]["missing"] == 1
    items = {i["name"]: i for i in state.queue.items(job["id"])}
    assert "midcsd.zip" in items["spyhunt"]["detail"]
    # hiding requires the RetroBat-closed confirmation
    job = run_job(state, state.queue.submit("mame_hide", "t", {"system": "mame"}))
    assert job["status"] == "failed"
    job = run_job(state, state.queue.submit("mame_hide", "t", {"system": "mame", "confirm_closed": True}))
    assert job["status"] == "done" and job["summary"]["add"] == 2
    hidden = {g.findtext("path") for g in ET.parse(folder / "gamelist.xml").getroot().findall("game")}
    assert hidden == {"./spyhuntr.zip", "./midssio.zip"}
    # bundle install: copies inner zips as-is, skips existing, re-hides
    bundle = make_zip(roms / "_source" / "devices.zip", {
        "midcsd.zip": zip_bytes({"csd.u7": DATA["csd.u7"]}),
        "midssio.zip": b"not used: already present"})
    job = run_job(state, state.queue.submit("mame_bundle", "t", {"archive": str(bundle), "system": "mame",
                                                                  "hide": True, "confirm_closed": True}))
    assert job["status"] == "done", job
    assert (folder / "midssio.zip").read_bytes() != b"not used: already present"
    job = run_job(state, state.queue.submit("mame_verify", "t", {"system": "mame"}))
    assert job["summary"]["good"] == 4


def test_arcade_pages(client, roms, tmp_path):
    assert "No reference data yet" in client.get("/arcade").text
    xml = roms / "_manager" / "uploads" / "m.xml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    xml.write_text(listxml())
    state = client.app.state.rlm
    run_job(state, state.queue.submit("mame_import", "t", {"xml": str(xml)}))
    z(roms / "mame", "spyhunt", "spyhunt.1", "spyhunt.2")
    r = client.get("/arcade/lookup", params={"q": "spy hunter"})
    assert "spyhunt" in r.text and "midssio" in r.text and "missing" in r.text
    r = client.post("/arcade/hide/preview", data={"system": "mame"})
    assert r.status_code == 200 and "Dry run" in r.text
    assert client.post("/arcade/hide", data={"system": "mame"}).status_code == 400
