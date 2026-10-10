"""BIOS manager with a synthetic catalog (dummy bytes, their real MD5s)."""
import hashlib

import pytest

from app import bios
from tests.conftest import make_zip, run_job

SYS = b"syscard-dummy" * 100
LYNX = b"lynxboot-dummy" * 10
FW = b"firmware-dummy"


def md5(b):
    return hashlib.md5(b).hexdigest()


@pytest.fixture()
def cat_state(state):
    text = f"""bios:
  - {{system: pcenginecd, file: syscard3.pce, md5: {md5(SYS)}, required: true}}
  - {{system: lynx, file: lynxboot.img, md5: {md5(LYNX)}, required: true}}
  - {{system: nds, file: firmware.bin, md5: "", required: false}}
  - {{system: psx, file: sub/scph5501.bin, md5: {md5(b'psx')}, required: true}}
"""
    state.settings.save_text(state.settings.bios_catalog_path, text, bios.parse_catalog)
    return state


def test_shipped_catalog_parses():
    from app.config import DATA_DIR

    cat = bios.load_catalog(DATA_DIR / "bios_catalog.yaml")
    keyed = {(e.system, e.file): e for e in cat}
    assert keyed[("pcenginecd", "syscard3.pce")].md5 == "38179df8f4ac870017db21ebcbf53114"
    assert keyed[("pcenginecd", "syscard3.pce")].required
    assert keyed[("nds", "firmware.bin")].md5 == ""
    assert len(cat) == 12


def test_scan_staged_matches_by_md5_and_name(cat_state, roms):
    d = roms / "_bios" / "pcenginecd"
    d.mkdir(parents=True)
    (d / "[BIOS] Super CD-ROM System (Japan) (v3.0).pce").write_bytes(SYS)
    (roms / "_bios" / "nds").mkdir()
    (roms / "_bios" / "nds" / "firmware.bin").write_bytes(FW)
    (roms / "_bios" / "lynx").mkdir()
    (roms / "_bios" / "lynx" / "lynxboot.img").write_bytes(b"wrong version")
    catalog = bios.load_catalog(cat_state.settings.bios_catalog_path)
    staged = bios.scan_staged(roms / "_bios", catalog, bios.Md5Cache(cat_state.db))
    by = {s.path.name: s for s in staged}
    assert [m.how for m in by["[BIOS] Super CD-ROM System (Japan) (v3.0).pce"].matches] == ["md5"]
    assert [m.how for m in by["firmware.bin"].matches] == ["name"]
    assert [m.how for m in by["lynxboot.img"].matches] == ["name-mismatch"]
    rows = bios.build_status(catalog, staged, None)
    summary = {d["system"]: d for d in bios.system_summary(rows)}
    assert summary["pcenginecd"]["req_staged"] == 1
    assert summary["lynx"]["missing_required"] == ["lynxboot.img"]


def test_export_bundle_and_powershell(cat_state, roms):
    d = roms / "_bios" / "pcenginecd"
    d.mkdir(parents=True)
    (d / "[BIOS] Super CD-ROM System (Japan) (v3.0).pce").write_bytes(SYS)
    job = run_job(cat_state, cat_state.queue.submit("bios_export", "t", {}))
    assert job["status"] == "done", job
    bundle = roms / "_bios" / "_install"
    assert (bundle / "syscard3.pce").read_bytes() == SYS
    script = (bundle / "install-bios.ps1").read_text()
    assert "$src = '\\\\jarvis\\roms\\_bios\\_install'" in script
    assert "$dst = 'G:\\RetroBat\\bios'" in script
    assert f"@('syscard3.pce', '{md5(SYS)}')" in script
    assert "Test-Path -LiteralPath $t" in script  # never overwrites on the desktop
    # the bundle folder is not itself scanned as staged BIOS
    catalog = bios.load_catalog(cat_state.settings.bios_catalog_path)
    assert all("_install" not in s.rel for s in bios.scan_staged(roms / "_bios", catalog, bios.Md5Cache(cat_state.db)))
    again = run_job(cat_state, cat_state.queue.submit("bios_export", "t", {}))
    assert again["summary"]["already_bundled"] == 1


def test_install_mounted_verifies_and_never_overwrites(cat_state, roms, tmp_path):
    desk = tmp_path / "desktop_bios"
    desk.mkdir()
    (desk / "lynxboot.img").write_bytes(b"something else")  # conflicting existing file
    cat_state.settings.update_config(lambda d: d["paths"].__setitem__("desktop_bios", str(desk)))
    cat_state.reload()
    (roms / "_bios" / "pcenginecd").mkdir(parents=True)
    (roms / "_bios" / "pcenginecd" / "sys.pce").write_bytes(SYS)
    (roms / "_bios" / "lynx").mkdir()
    (roms / "_bios" / "lynx" / "boot.img").write_bytes(LYNX)
    (roms / "_bios" / "psx").mkdir()
    (roms / "_bios" / "psx" / "x.bin").write_bytes(b"psx")
    job = run_job(cat_state, cat_state.queue.submit("bios_install", "t", {"files": []}))
    assert job["status"] == "done", job
    assert (desk / "syscard3.pce").read_bytes() == SYS
    assert (desk / "sub" / "scph5501.bin").read_bytes() == b"psx"  # sub-folder target created
    assert (desk / "lynxboot.img").read_bytes() == b"something else"
    assert job["summary"]["installed"] == 2 and job["summary"]["conflict"] == 1


def test_stage_from_archive(cat_state, roms):
    src = make_zip(roms / "_source" / "bios-pack.zip", {
        "PCE/[BIOS] Super CD-ROM System (Japan) (v3.0).pce": SYS, "other/readme.txt": b"x", "Lynx/lynxboot.img": LYNX})
    catalog = bios.load_catalog(cat_state.settings.bios_catalog_path)
    members = bios.scan_archive(src, catalog)
    assert sum(1 for m in members if m.matches) == 2
    job = run_job(cat_state, cat_state.queue.submit("bios_stage", "t", {"archive": str(src)}))
    assert job["summary"]["staged"] == 2
    assert (roms / "_bios" / "pcenginecd" / "[BIOS] Super CD-ROM System (Japan) (v3.0).pce").exists()
    assert (roms / "_bios" / "lynx" / "lynxboot.img").read_bytes() == LYNX


def test_parse_retrobat_style_xml_and_merge(cat_state):
    xml = """<?xml version="1.0"?>
<biosList>
  <system name="3do">
    <bios path="bios/panafz10.bin" md5="51f2f43ae2f3508a14d9f56597e2d3ce" description="Panasonic FZ-10" />
  </system>
  <system name="pcenginecd">
    <bios path="bios/syscard3.pce" md5="38179df8f4ac870017db21ebcbf53114" />
  </system>
  <system name="broken"><bios path="x.bin" md5="nothex" /></system>
</biosList>"""
    parsed = bios.parse_definition(xml)
    assert {(e.system, e.file) for e in parsed} == {("3do", "panafz10.bin"), ("pcenginecd", "syscard3.pce")}
    catalog = bios.load_catalog(cat_state.settings.bios_catalog_path)
    new = bios.merge_new(catalog, parsed)
    assert [(e.system, e.file) for e in new] == [("3do", "panafz10.bin")]
    text = bios.append_to_catalog_file(cat_state.settings.bios_catalog_path, new)
    merged = bios.parse_catalog(text)
    assert len(merged) == len(catalog) + 1 and merged[-1].note == "Panasonic FZ-10"


def test_bios_page_renders(client, roms):
    (roms / "_bios" / "gb").mkdir(parents=True)
    (roms / "_bios" / "gb" / "gb_bios.bin").write_bytes(b"x")
    r = client.get("/bios")
    assert r.status_code == 200 and "gb_bios.bin" in r.text and "export mode" in r.text
    r = client.get("/bios/install/preview", params={"mode": "export"})
    assert "Dry run" in r.text
