"""Work out which system each file in an unknown archive or folder belongs to.

Signals, strongest first:
  1. file headers (Genesis/32X carts, Sega CD/Saturn/Dreamcast/PC Engine CD,
     PlayStation/PSP/GameCube/Wii disc images)
  2. an extension only one system uses
  3. the files inside a per-game .zip/.7z (sampled per folder, so large sets stay fast)
  4. folder names such as "SNES/" or "Genesis/" as a tie-breaker
Anything still ambiguous is left for the operator to assign.
"""
from __future__ import annotations

import io
import re
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import archives, chd
from .systems import ARCHIVE_EXTS, System

FOLDER_ALIASES = {
    "gameboy": "gb", "game boy": "gb", "game boy color": "gbc", "gameboy color": "gbc",
    "game boy advance": "gba", "gameboy advance": "gba", "famicom": "nes", "nintendo": "nes",
    "super nintendo": "snes", "super famicom": "snes", "sfc": "snes", "nintendo 64": "n64",
    "ds": "nds", "nintendo ds": "nds", "nintendo 3ds": "3ds", "gc": "gamecube", "ngc": "gamecube",
    "ps1": "psx", "playstation": "psx", "playstation 1": "psx", "playstation 2": "ps2",
    "genesis": "megadrive", "mega drive": "megadrive", "md": "megadrive", "master system": "mastersystem",
    "sms": "mastersystem", "game gear": "gamegear", "gg": "gamegear", "sega cd": "segacd", "mega-cd": "segacd",
    "mega cd": "segacd", "megacd": "segacd", "32x": "sega32x", "dc": "dreamcast", "pc engine": "pcengine",
    "turbografx": "pcengine", "turbografx-16": "pcengine", "turbografx 16": "pcengine", "tg16": "pcengine",
    "pc engine cd": "pcenginecd", "turbografx-cd": "pcenginecd", "turbografx cd": "pcenginecd",
    "tgcd": "pcenginecd", "neo geo": "neogeo", "2600": "atari2600", "atari 2600": "atari2600",
    "5200": "atari5200", "atari 5200": "atari5200", "7800": "atari7800", "atari 7800": "atari7800",
    "atari lynx": "lynx", "arcade": "mame", "fba": "fbneo",
}

SNIFF_BYTES = 64 * 1024
MAX_DATA_OFFSET = 16 * 1024 * 1024  # never read further than this into a track to find its header
INNER_PEEK_MAX = 512 * 1024 * 1024
INNER_SAMPLE = 5


@dataclass
class Ident:
    entry: str
    size: int
    system: str | None            # decided system, or None
    candidates: list[str] = field(default_factory=list)  # when ambiguous
    suggestion: str | None = None
    how: str = ""
    tracks: int = 0               # .cue/.gdi: number of track files that go with it


# ---- header sniffing ------------------------------------------------------------------

def sniff_cart(data: bytes) -> str | None:
    hdr = data[0x100:0x120]
    if hdr.startswith(b"SEGA 32X"):
        return "sega32x"
    if hdr.startswith(b"SEGA"):
        return "megadrive"
    return None


def sniff_disc(data: bytes, total_size: int = 0) -> tuple[str | None, list[str], str]:
    """(system, candidates, how) from the first bytes of a disc image or data track."""
    if data[0x1C:0x20] == b"\xc2\x33\x9f\x3d":
        return "gamecube", [], "GameCube disc header"
    if data[0x18:0x1C] == b"\x5d\x1c\x9e\xa3":
        return "wii", [], "Wii disc header"
    for marker, system in ((b"SEGADISCSYSTEM", "segacd"), (b"SEGA SEGASATURN", "saturn"),
                           (b"SEGA SEGAKATANA", "dreamcast"), (b"PC Engine CD-ROM SYSTEM", "pcenginecd"),
                           (b"PSP GAME", "psp")):
        if marker in data:
            return system, [], f"{marker.decode()} boot header"
    pos = data.find(b"CD001")
    if pos >= 0 and b"PLAYSTATION" in data[pos:pos + 64]:
        if total_size > 800 * 1024 * 1024:
            return "ps2", [], "PlayStation volume, DVD-sized"
        return None, ["psx", "ps2"], "PlayStation volume descriptor (PS1 or a CD-based PS2 game)"
    return None, [], ""


_MSF = re.compile(r"(\d+):(\d+):(\d+)")


def cue_data_tracks(text: str) -> list[tuple[str, int]]:
    """(file name, byte offset of INDEX 01) for every data (non-AUDIO) track of a .cue.

    Redump-style sheets put a 2-second pregap at the start of a data track's .bin
    (e.g. PC Engine CD track 2), so the disc header is not at byte 0 of the file.
    """
    out: list[tuple[str, int]] = []
    cur_file, mode, sector = None, None, 2352
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        kw = words[0].upper()
        if kw == "FILE":
            refs = chd.descriptor_refs(line, ".cue")
            cur_file = refs[0] if refs else None
        elif kw == "TRACK" and len(words) >= 3:
            mode = words[2].upper()
            sector = 2048 if mode.endswith("/2048") else 2336 if mode.endswith("/2336") else 2352
        elif kw == "INDEX" and len(words) >= 3 and words[1] == "01" and cur_file and mode and mode != "AUDIO":
            m = _MSF.fullmatch(words[2])
            if m:
                frames = (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))
                out.append((cur_file, frames * sector))
    return out


# ---- helpers ------------------------------------------------------------------------------

def _owners(ext: str, systems: dict[str, System]) -> list[str]:
    return [k for k, s in systems.items() if not s.arcade and ext in s.rom_extensions]


def primary_owner(ext: str, owners: list[str], systems: dict[str, System]) -> str | None:
    """Several systems accept .gb, but it is only gb's *first-listed* (primary) format."""
    primary = [k for k in owners if systems[k].extensions and systems[k].extensions[0] == ext]
    return primary[0] if len(primary) == 1 else None


def folder_hint(entry: str, systems: dict[str, System]) -> str | None:
    for part in reversed(entry.replace("\\", "/").split("/")[:-1]):
        key = part.strip().casefold()
        if key in systems:
            return key
        alias = FOLDER_ALIASES.get(key)
        if alias in systems:
            return alias
    return None


def _inner_names(source: Path, entry: archives.Entry) -> list[str] | None:
    """File names inside a per-game archive (None when it is too big or unreadable)."""
    if entry.size > INNER_PEEK_MAX:
        return None
    try:
        data = archives.read_member(source, entry.name, INNER_PEEK_MAX)
        if entry.ext == ".zip":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return [n for n in zf.namelist() if not n.endswith("/")]
        with tempfile.TemporaryDirectory(prefix="rlm-id-") as tmp:
            p = Path(tmp) / "inner.7z"
            p.write_bytes(data)
            return [e.name for e in archives.list_7z(p) if not e.is_dir]
    except Exception:  # noqa: BLE001 - unreadable inner archive: undecided
        return None


def _classify_names(names: list[str], systems: dict[str, System]) -> tuple[str | None, list[str], str]:
    exts = Counter(Path(n).suffix.lower() for n in names)
    exts.pop(".txt", None)
    exts.pop(".nfo", None)
    if not exts:
        return None, [], "empty archive"
    if exts.keys() & {".cue", ".gdi", ".ccd"}:
        disc = [k for k, s in systems.items() if s.disc]
        if ".gdi" in exts:
            return "dreamcast", [], "contains a .gdi disc set"
        return None, disc, "contains a disc set (.cue/.bin)"
    owners_sets = [set(_owners(e, systems)) for e in exts if _owners(e, systems)]
    if owners_sets:
        common = set.intersection(*owners_sets)
        if len(common) == 1:
            return common.pop(), [], f"contains {', '.join(sorted(exts))} files"
        if len(exts) == 1:
            ext = next(iter(exts))
            prim = primary_owner(ext, sorted(common), systems)
            if prim:
                return prim, [], f"contains {ext} files ({prim}'s primary format)"
        cand = sorted(common or set.union(*owners_sets))
        return None, cand, f"contains {', '.join(sorted(exts))} files"
    arcade = [k for k, s in systems.items() if s.arcade]
    return None, arcade, "no cartridge/disc files inside: looks like an arcade ROM set"


# ---- main -----------------------------------------------------------------------------------

def identify(source: Path, systems: dict[str, System], ignore: set[str] | None = None,
             time_budget: float = 120.0) -> list[Ident]:
    from .extract import disc_groups  # local import: extract imports this module's neighbours

    source = Path(source)
    ignore = ignore or {".txt", ".nfo"}
    entries = [e for e in archives.list_entries(source) if not e.is_dir]
    groups, owner, _odd = disc_groups(source, entries)
    by_name = {e.name: e for e in entries}
    deadline = time.monotonic() + time_budget
    out: list[Ident] = []
    inner_by_folder: dict[str, list[archives.Entry]] = defaultdict(list)

    for e in entries:
        if e.ext in ignore or e.name in owner:
            continue
        if e.ext in ARCHIVE_EXTS:
            inner_by_folder[e.name.rsplit("/", 1)[0] if "/" in e.name else ""].append(e)
            continue
        out.append(_identify_file(source, e, systems, groups, by_name))

    # Per-game archives: peek into a few per folder; if they agree, the rest of the folder follows.
    for folder, items in inner_by_folder.items():
        sampled: list[tuple[archives.Entry, tuple]] = []
        for e in items[:INNER_SAMPLE]:
            names = _inner_names(source, e) if time.monotonic() < deadline else None
            sampled.append((e, _classify_names(names, systems) if names is not None else (None, [], "")))
        decided = {r[0] for _e, r in sampled if r[0]}
        uniform = len(decided) == 1 and all(r[0] for _e, r in sampled)
        for e, r in sampled:
            out.append(_finish(e, r, systems))
        for e in items[INNER_SAMPLE:]:
            if uniform:
                system = next(iter(decided))
                out.append(Ident(e.name, e.size, system, how=f"same as the {len(sampled)} sampled archives in this folder"))
            elif time.monotonic() < deadline:
                names = _inner_names(source, e)
                r = _classify_names(names, systems) if names is not None else (None, [], "")
                out.append(_finish(e, r, systems))
            else:
                out.append(_finish(e, (None, [], "not inspected (time budget used up)"), systems))
    return sorted(out, key=lambda i: i.entry.casefold())


def _finish(e: archives.Entry, r: tuple, systems: dict[str, System]) -> Ident:
    system, cand, how = r
    if system:
        return Ident(e.name, e.size, system, how=how)
    hint = folder_hint(e.name, systems)
    if hint and (not cand or hint in cand):
        return Ident(e.name, e.size, hint, how=f"folder name ({how})" if how else "folder name")
    return Ident(e.name, e.size, None, cand, None, how or "could not look inside")


def _identify_file(source: Path, e: archives.Entry, systems: dict[str, System], groups: dict,
                   by_name: dict) -> Ident:
    ext = e.ext
    owners = _owners(ext, systems)
    if e.name in groups:  # .cue / .gdi with its tracks
        members, _missing = groups[e.name]
        total = e.size + sum(by_name[m].size for m in members if m in by_name)
        probes: list[tuple[str, int]] = []
        if ext == ".cue":
            try:
                text = archives.read_member(source, e.name, 1024 * 1024).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                text = ""
            by_base = {m.rsplit("/", 1)[-1].casefold(): m for m in members}
            for fname, offset in cue_data_tracks(text):
                member = by_base.get(fname.replace("\\", "/").rsplit("/", 1)[-1].casefold())
                if member and offset <= MAX_DATA_OFFSET:
                    probes.append((member, offset))
        probes += [(m, 0) for m in members[:3] if (m, 0) not in probes]
        for member, offset in probes[:6]:
            try:
                data = archives.read_member(source, member, offset + SNIFF_BYTES)[offset:]
            except Exception:  # noqa: BLE001
                continue
            system, cand, how = sniff_disc(data, total)
            if system or cand:
                return _with_hint(Ident(e.name, total, system, cand, how=how, tracks=len(members)), systems)
        disc = [k for k in owners if systems[k].disc]
        if len(disc) == 1:
            return Ident(e.name, total, disc[0], how=f"{ext} disc set", tracks=len(members))
        return _with_hint(Ident(e.name, total, None, disc, how="disc set without a recognised header",
                                tracks=len(members)), systems)
    if ext == ".iso":
        try:
            system, cand, how = sniff_disc(archives.read_member(source, e.name, SNIFF_BYTES), e.size)
        except Exception:  # noqa: BLE001
            system, cand, how = None, [], ""
        if system or cand:
            return _with_hint(Ident(e.name, e.size, system, cand, how=how), systems)
    if ext == ".bin" and e.size > 0x200:
        try:
            system = sniff_cart(archives.read_member(source, e.name, 0x200))
        except Exception:  # noqa: BLE001
            system = None
        if system:
            return Ident(e.name, e.size, system, how="SEGA cartridge header")
    if len(owners) == 1:
        return Ident(e.name, e.size, owners[0], how=f"{ext} extension")
    hint = folder_hint(e.name, systems)
    if hint in owners:
        return Ident(e.name, e.size, hint, how=f"folder name ({ext} fits {hint})")
    prim = primary_owner(ext, owners, systems)
    if prim:
        return Ident(e.name, e.size, prim, how=f"{ext} extension ({prim}'s primary format)")
    if not owners:
        return Ident(e.name, e.size, None, [], how=f"unrecognised extension {ext or '(none)'}")
    ident = Ident(e.name, e.size, None, owners, how=f"{ext} is used by {len(owners)} systems")
    if ext == ".bin" and e.size <= 48 * 1024:
        ident.candidates = [k for k in ("atari2600", "atari5200", "atari7800") if k in systems] or owners
        ident.how = "small .bin with no console header: probably an Atari cartridge"
    return _with_hint(ident, systems)


def _with_hint(ident: Ident, systems: dict[str, System]) -> Ident:
    if ident.system:
        return ident
    hint = folder_hint(ident.entry, systems)
    if hint and (not ident.candidates or hint in ident.candidates):
        ident.system, ident.how = hint, f"folder name ({ident.how})" if ident.how else "folder name"
    elif ident.candidates:
        ident.suggestion = ident.candidates[0] if len(ident.candidates) == 1 else None
    return ident


def summarize(idents: list[Ident]) -> tuple[dict[str, list[Ident]], dict[tuple, list[Ident]], list[Ident]]:
    """(decided by system, ambiguous grouped by candidates, unknown)."""
    decided: dict[str, list[Ident]] = defaultdict(list)
    ambiguous: dict[tuple, list[Ident]] = defaultdict(list)
    unknown: list[Ident] = []
    for i in idents:
        if i.system:
            decided[i.system].append(i)
        elif i.candidates:
            ambiguous[tuple(i.candidates)].append(i)
        else:
            unknown.append(i)
    return dict(sorted(decided.items())), dict(ambiguous), unknown
