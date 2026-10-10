"""Atari Lynx: turn raw .lyx dumps into headered .lnx files.

LNX header, 64 bytes, little-endian:
    0   4  magic "LYNX"
    4   2  bank 0 page size  (file size / 256)
    6   2  bank 1 page size  (0)
    8   2  version           (1)
    10  32 cartridge name    (NUL padded)
    42  16 manufacturer      (NUL padded)
    58  1  rotation          (0 none, 1 left, 2 right)
    59  5  spare             (0)

The .lyx files are kept so that re-runs see the game as already present.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

from .. import fsutil

HEADER_SIZE = 64
ROTATION = {"none": 0, "left": 1, "right": 2}
ROTATION_NAMES = {v: k for k, v in ROTATION.items()}


def make_header(rom_size: int, name: str, manufacturer: str = "", rotation: str = "none") -> bytes:
    if rom_size <= 0 or rom_size % 256:
        raise ValueError(f"Lynx ROM size {rom_size} is not a multiple of 256 bytes")
    pages = rom_size // 256
    if pages > 0xFFFF:
        raise ValueError(f"Lynx ROM too large ({rom_size} bytes)")
    hdr = struct.pack(
        "<4sHHH32s16sB5s",
        b"LYNX",
        pages,
        0,
        1,
        name.encode("ascii", "replace")[:32],
        manufacturer.encode("ascii", "replace")[:16],
        ROTATION[rotation],
        b"\0" * 5,
    )
    assert len(hdr) == HEADER_SIZE
    return hdr


def read_rotation(lnx: Path) -> str | None:
    try:
        with open(lnx, "rb") as f:
            hdr = f.read(HEADER_SIZE)
    except OSError:
        return None
    if len(hdr) < HEADER_SIZE or hdr[:4] != b"LYNX":
        return None
    return ROTATION_NAMES.get(hdr[58], f"unknown({hdr[58]})")


def rotation_for(title: str, overrides: dict[str, str]) -> str:
    """Longest configured title prefix wins (case-insensitive)."""
    best, best_len = "none", -1
    t = title.casefold()
    for key, rot in overrides.items():
        k = key.casefold()
        if t.startswith(k) and len(k) > best_len:
            best, best_len = rot, len(k)
    return best


def cart_name(stem: str) -> str:
    # Drop No-Intro tags: "Klax (USA, Europe)" -> "Klax"
    return stem.split(" (")[0].strip() or stem


def write_lnx(lyx: Path, lnx: Path, manufacturer: str = "", rotation: str = "none") -> str:
    """Create ``lnx`` from ``lyx``. Never overwrites. Returns a short description."""
    data = Path(lyx).read_bytes()
    if data[:4] == b"LYNX":
        body = data  # already headered despite the extension
        note = "source already had an LNX header; copied"
    else:
        body = make_header(len(data), cart_name(Path(lyx).stem), manufacturer, rotation) + data
        note = f"header added ({len(data) // 256} pages, rotation {rotation})"
    import io

    fsutil.write_stream_new(io.BytesIO(body), lnx)
    return note


def run(ctx, cfg, folder: Path) -> None:
    """Post-extraction hook: give every .lyx without a .lnx twin a header."""
    if not folder.is_dir():
        return
    names = {e.name.casefold(): e.name for e in os.scandir(folder) if e.is_file()}
    for low, name in sorted(names.items()):
        if not low.endswith(".lyx"):
            continue
        stem = name[:-4]
        if (stem + ".lnx").casefold() in names:
            continue
        ctx.check()
        lyx = folder / name
        rot = rotation_for(stem, cfg.lynx.rotation)
        try:
            note = write_lnx(lyx, folder / (stem + ".lnx"), cfg.lynx.manufacturer, rot)
            ctx.log(f"lynx: {stem}.lnx {note}")
            ctx.count("lnx_created")
            ctx.item(stem + ".lnx", "added", note)
        except (ValueError, OSError) as e:
            ctx.log(f"lynx: {name}: {e}")
            ctx.count("failed")
            ctx.item(name, "failed", str(e))
