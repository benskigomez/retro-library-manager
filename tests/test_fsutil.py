import os
import stat
from pathlib import Path

import pytest

from app import fsutil


def test_write_never_overwrites(tmp_path):
    (tmp_path / "a").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        fsutil.copy_new(tmp_path / "a", tmp_path / "a")
    src = tmp_path / "src"
    src.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        fsutil.copy_new(src, tmp_path / "a")
    assert (tmp_path / "a").read_bytes() == b"old"
    assert not list(tmp_path.glob(".rlm-part-*"))


def test_safe_move_and_copy_verify(tmp_path):
    src = tmp_path / "x" / "f.bin"
    src.parent.mkdir()
    src.write_bytes(b"data" * 100)
    md5 = fsutil.copy_verify(src, tmp_path / "copy.bin")
    assert md5 == fsutil.md5_file(src)
    fsutil.safe_move(src, tmp_path / "y" / "f.bin")
    assert not src.exists() and (tmp_path / "y" / "f.bin").read_bytes() == b"data" * 100
    with pytest.raises(FileExistsError):
        fsutil.safe_move(tmp_path / "copy.bin", tmp_path / "y" / "f.bin")


def test_cross_share_detection():
    assert not fsutil._same_top(Path("/mnt/user/roms/a"), Path("/mnt/user/isos/b"))
    assert fsutil._same_top(Path("/mnt/user/roms/a"), Path("/mnt/user/roms/_trash/b"))


def test_trash_and_restore_keep_relative_paths(tmp_path):
    roms = tmp_path / "roms"
    (roms / "gb").mkdir(parents=True)
    f = roms / "gb" / "G.gb"
    f.write_bytes(b"g")
    batch = fsutil.new_trash_batch(roms / "_trash")
    dest = fsutil.move_to_trash(f, roms, batch)
    assert dest == batch / "gb" / "G.gb" and not f.exists()
    f.write_bytes(b"replacement")  # restore must not clobber it
    restored, skipped = fsutil.restore_from_trash(batch, roms)
    assert restored == [] and skipped == [str(Path("gb") / "G.gb")]
    assert f.read_bytes() == b"replacement"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_fix_perms_a_plus_rwX(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    f = d / "f"
    f.write_bytes(b"x")
    os.chmod(f, 0o600)
    fsutil.fix_perms(d, recursive=True)
    assert stat.S_IMODE(d.stat().st_mode) == 0o777
    assert stat.S_IMODE(f.stat().st_mode) == 0o666
