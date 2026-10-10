"""Every page renders against a populated library (catches template errors)."""
from tests.conftest import make_zip, run_job
from tests.test_arcade import listxml


def test_all_pages_render(client, roms, tmp_path):
    state = client.app.state.rlm
    make_zip(roms / "_source" / "gb-roms.zip", {"Tetris (World).gb": b"\0" * 64, "[BIOS] GB (World).gb": b"b"})
    run_job(state, state.queue.submit("extract", "x", {"archive": str(roms / "_source" / "gb-roms.zip"),
                                                       "system": "gb", "import_mode": "none"}, system="gb"))
    (roms / "lynx").mkdir()
    (roms / "lynx" / "Klax (USA).lyx").write_bytes(b"\0" * 512)
    xml = tmp_path / "m.xml"
    xml.write_text(listxml())
    run_job(state, state.queue.submit("mame_import", "x", {"xml": str(xml)}))
    make_zip(roms / "mame" / "spyhunt.zip", {"a": b"a"})
    verify = run_job(state, state.queue.submit("mame_verify", "x", {"system": "mame"}))
    (roms / "gb" / "Junk.foo").write_bytes(b"x")
    client.post("/system/gb/trash", data={"names": ["Junk.foo"]})
    batch = next((roms / "_trash").iterdir()).name

    pages = [
        "/", "/?rescan=1", "/system/gb", "/system/gb?show=flagged", "/system/lynx", "/system/mame",
        "/import", "/import?system=gb", "/chd", "/chd?system=psx", "/bios", "/arcade",
        f"/arcade/results/{verify['id']}", f"/arcade/results/{verify['id']}?status=unknown",
        "/jobs", f"/jobs/{verify['id']}", f"/jobs/{verify['id']}/status", "/trash", f"/trash/{batch}",
        f"/trash/{batch}?action=restore", f"/trash/{batch}?action=purge",
        "/settings", "/settings?which=systems", "/settings?which=bios_catalog", "/settings/filters/gb",
        "/extract/lynx", "/files/browse", f"/files/browse?path={roms}", "/arcade/lookup?q=spy",
        "/bios/install/preview?mode=export", "/bios/install/preview?mode=mounted",
    ]
    for url in pages:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:500])
    assert "Tetris" in client.get("/system/gb").text
    assert "1 BIOS staged" in client.get("/").text
