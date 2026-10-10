import time

from tests.conftest import make_zip


def test_auth_required(client):
    r = client.get("/", auth=None)
    assert r.status_code == 401
    assert "Basic" in r.headers["www-authenticate"]
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/healthz", auth=None).status_code == 200


def test_dashboard_reads_library(client, roms):
    (roms / "gb").mkdir()
    (roms / "gb" / "Tetris (World).gb").write_bytes(b"x" * 10)
    (roms / "gb" / "Other (USA).gb").write_bytes(b"x" * 10)
    (roms / "gb" / "Packed (USA).zip").write_bytes(b"PK")
    (roms / "lynx").mkdir()
    (roms / "lynx" / "Game (USA).lyx").write_bytes(b"x")
    (roms / "lynx" / "Game (USA).lnx").write_bytes(b"x")
    (roms / "lynx" / "Weird.foo").write_bytes(b"x")
    make_zip(roms / "_source" / "lynx-roms.zip", {"a.lyx": b"1"})
    (roms / "MysteryFolder").mkdir()
    r = client.get("/?rescan=1")
    assert r.status_code == 200
    body = r.text
    assert "Missing sources" in body and "gb · 2 games" in body  # gb has games but no source
    assert "MysteryFolder" in body
    assert "archive(s) where loose ROMs are expected" in body
    assert ".foo" in body
    r = client.get("/system/lynx")
    assert r.status_code == 200 and "Weird.foo" in r.text


def test_job_queue_sse_and_history(client):
    state = client.app.state.rlm

    @state.queue.register("demo")
    def demo(ctx):
        for i in range(3):
            ctx.log(f"step {i}")
            ctx.count("added")
        ctx.item("thing", "added", "ok")
        return {"note": "fine"}

    job_id = state.queue.submit("demo", "Demo job", {"x": 1}, system="gb")
    for _ in range(50):
        if state.queue.get(job_id)["status"] == "done":
            break
        time.sleep(0.1)
    job = state.queue.get(job_id)
    assert job["status"] == "done" and job["summary"]["added"] == 3
    with client.stream("GET", f"/jobs/{job_id}/stream") as r:
        text = "".join(r.iter_text())
    assert "step 2" in text and "event: end" in text
    assert client.get("/jobs").status_code == 200
    page = client.get(f"/jobs/{job_id}").text
    assert "Demo job" in page and "thing" in page


def test_cancel_running_job(client):
    state = client.app.state.rlm

    @state.queue.register("slow")
    def slow(ctx):
        while True:
            ctx.check()
            time.sleep(0.05)

    job_id = state.queue.submit("slow", "Slow", {})
    for _ in range(50):
        if state.queue.get(job_id)["status"] == "running":
            break
        time.sleep(0.05)
    client.post(f"/jobs/{job_id}/cancel")
    for _ in range(50):
        if state.queue.get(job_id)["status"] == "cancelled":
            break
        time.sleep(0.1)
    assert state.queue.get(job_id)["status"] == "cancelled"


def test_settings_validation_and_backup(client, settings):
    bad = client.post("/settings/config", data={"text": "jobs:\n  workers: 99\n"})
    assert "Not saved" in bad.text
    text = settings.config_path.read_text()
    ok = client.post("/settings/config", data={"text": text.replace("wait_seconds: 0", "wait_seconds: 1")})
    assert ok.status_code in (200, 303)
    assert settings.config.stability.wait_seconds == 1
    assert list(settings.config_path.parent.glob("config.yaml.bak-*"))


def test_per_system_filter_editing_keeps_comments(client, settings):
    r = client.post("/settings/filters/snes", data={"allow": "Terranigma (Europe)\n", "deny": ""})
    assert r.status_code in (200, 303)
    assert settings.config.filters.systems["snes"].allow == ["Terranigma (Europe)"]
    assert "# GoodTools region codes" in settings.config_path.read_text()
    r = client.post("/settings/filters/snes/test", data={"names": "Terranigma (Europe).sfc\nX (Europe).sfc"})
    assert "allow-list" in r.text


def test_trash_from_system_page_and_restore(client, roms):
    (roms / "gb").mkdir()
    (roms / "gb" / "Bad [x].gb").write_bytes(b"bad")
    r = client.post("/system/gb/trash/preview", data={"names": ["Bad [x].gb"]})
    assert "1 to trash" in r.text
    client.post("/system/gb/trash", data={"names": ["Bad [x].gb"]})
    assert not (roms / "gb" / "Bad [x].gb").exists()
    batches = list((roms / "_trash").iterdir())
    assert (batches[0] / "gb" / "Bad [x].gb").read_bytes() == b"bad"
    client.post(f"/trash/{batches[0].name}/restore")
    assert (roms / "gb" / "Bad [x].gb").read_bytes() == b"bad"


def test_password_change(client):
    r = client.post("/settings/password/change", data={"current": "test-password", "new": "newpassword1", "confirm": "newpassword1"})
    assert "Password changed" in r.text
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "newpassword1")).status_code == 200
