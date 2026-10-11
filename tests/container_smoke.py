"""Smoke test run INSIDE the built Docker image by CI, against the real tools.

The pytest suite uses py7zr and a fake chdman, so it cannot catch problems with
the 7zz / unzip / chdman versions the image actually ships (Debian packages).
This script exercises those tools through the app's own code paths.

    docker run --rm -i --user 99:100 <image> python - < tests/container_smoke.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, "/app")
from app import archives  # noqa: E402

failures = []


def check(name, fn):
    try:
        fn()
        print(f"ok    {name}")
    except Exception as e:  # noqa: BLE001
        failures.append(name)
        print(f"FAIL  {name}: {type(e).__name__}: {e}")


work = Path(tempfile.mkdtemp(prefix="smoke-"))
for tool in ("7zz", "unzip", "zipinfo", "chdman"):
    check(f"{tool} on PATH", lambda t=tool: shutil.which(t) or (_ for _ in ()).throw(RuntimeError("missing")))

# --- 7-Zip: build a per-game .7z with the real 7zz, then use every app path that reads 7z ---
src_dir = work / "src"
src_dir.mkdir()
(src_dir / "Game [!] (USA).32x").write_bytes(b"\x01" * 4096)
seven = work / "Game (USA).7z"
subprocess.run(["7zz", "a", "-bd", str(seven), str(src_dir / "Game [!] (USA).32x")], check=True, capture_output=True)


def t_list_7z():
    names = [e.name for e in archives.list_7z(seven)]
    assert names == ["Game [!] (USA).32x"], names


def t_extract_all_7z():
    out = work / "x7"
    archives.extract_all(seven, out)
    assert (out / "Game [!] (USA).32x").read_bytes() == b"\x01" * 4096


def t_stream_member_7z():
    data = b"".join(archives.stream_member(seven, "Game [!] (USA).32x"))
    assert data == b"\x01" * 4096


def t_read_member_7z_early_stop():
    assert archives.read_member(seven, "Game [!] (USA).32x", 100) == b"\x01" * 100


check("7zz list", t_list_7z)
check("7zz extract_all (per-game .7z unpack)", t_extract_all_7z)
check("7zz stream_member (-so -spd)", t_stream_member_7z)
check("7zz read_member stops early", t_read_member_7z_early_stop)


# --- the full extraction engine on a zip of per-game .7z files (the 32X layout) ---
def t_engine_inner_7z():
    from app.config import Config
    from app.extract import execute
    from app.systems import System

    roms = work / "roms"
    outer = work / "set.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("Game (USA).7z", seven.read_bytes())
    cfg = Config()
    cfg.paths.roms = str(roms)
    cfg.paths.temp = str(work / "tmp")
    cfg.stability.wait_seconds = 0
    sysdef = System(key="sega32x", name="32X", extensions=[".32x"])

    class Ctx:
        id = 1

        def __init__(self):
            self.counts = {}

        def log(self, m):
            print("      " + m)

        def count(self, k, n=1):
            self.counts[k] = self.counts.get(k, 0) + n

        def item(self, *a, **k):
            pass

        def check(self):
            pass

    ctx = Ctx()
    execute(ctx, cfg, sysdef, outer)
    assert ctx.counts.get("failed", 0) == 0, ctx.counts
    assert (roms / "sega32x" / "Game [!] (USA).32x").exists(), list((roms / "sega32x").iterdir())


check("engine: zip of per-game .7z -> unpacked ROM", t_engine_inner_7z)


# --- Info-ZIP unzip backend with glob metacharacters ---
def t_unzip_backend():
    z = work / "u.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Army Men [!] (U).z64", b"z" * 100)
    archives.configure_tools("7zz", "unzip", "unzip")
    try:
        archives.extract_zip_member(z, "Army Men [!] (U).z64", work / "Army Men [!] (U).z64")
    finally:
        archives.configure_tools("7zz", "unzip", "python")
    assert (work / "Army Men [!] (U).z64").read_bytes() == b"z" * 100


check("unzip -p with escaped [!]", t_unzip_backend)


# --- chdman: createcd then extractcd round trip ---
def t_chdman():
    d = work / "disc"
    d.mkdir()
    (d / "g.bin").write_bytes(os.urandom(2352 * 300))
    (d / "g.cue").write_text('FILE "g.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    subprocess.run(["chdman", "createcd", "-i", str(d / "g.cue"), "-o", str(d / "g.chd")], check=True,
                   capture_output=True)
    subprocess.run(["chdman", "extractcd", "-i", str(d / "g.chd"), "-o", str(d / "out.cue"), "-ob",
                    str(d / "out.bin")], check=True, capture_output=True)
    assert (d / "out.bin").read_bytes() == (d / "g.bin").read_bytes()


check("chdman createcd/extractcd round trip", t_chdman)

shutil.rmtree(work, ignore_errors=True)
print(f"\n{len(failures)} failure(s)" if failures else "\nall container checks passed")
sys.exit(1 if failures else 0)
