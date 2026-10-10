"""MAME reference data (from `mame -listxml`) and set verification.

Verification reads only zip central directories (CRC32 + size), so it is fast
even over SMB. A set is "good" when every required ROM's CRC is found in the
set itself, its parent / BIOS chain (split and merged sets) or the zip of the
device that owns it (e.g. Spy Hunter needs midssio.zip and midcsd.zip).
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .. import archives
from ..db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS machines (
    name TEXT PRIMARY KEY, description TEXT, year TEXT, manufacturer TEXT,
    cloneof TEXT, romof TEXT, isbios INTEGER, isdevice INTEGER, ismechanical INTEGER,
    runnable INTEGER, has_disk INTEGER, default_bios TEXT, sourcefile TEXT
);
CREATE TABLE IF NOT EXISTS roms (
    machine TEXT, name TEXT, size INTEGER, crc TEXT, merge TEXT, status TEXT, optional INTEGER, bios TEXT
);
CREATE INDEX IF NOT EXISTS roms_machine ON roms(machine);
CREATE TABLE IF NOT EXISTS device_refs (machine TEXT, device TEXT);
CREATE INDEX IF NOT EXISTS device_refs_machine ON device_refs(machine);
CREATE INDEX IF NOT EXISTS machines_desc ON machines(description);
"""

HIDE_CLASSES = ("bios", "device", "mechanical")


def open_db(path: Path) -> Database:
    return Database(path, SCHEMA)


# ---- import ----------------------------------------------------------------------

def import_listxml(db: Database, xml_path: Path, progress=None) -> dict:
    """Replace the reference data with a -listxml dump (streamed; ~250 MB is fine)."""
    machines = roms = devices = 0
    build = ""
    with db.tx() as c:
        for t in ("machines", "roms", "device_refs", "meta"):
            c.execute(f"DELETE FROM {t}")
        root = None
        m_rows, r_rows, d_rows = [], [], []

        def flush():
            c.executemany("INSERT OR REPLACE INTO machines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", m_rows)
            c.executemany("INSERT INTO roms VALUES (?,?,?,?,?,?,?,?)", r_rows)
            c.executemany("INSERT INTO device_refs VALUES (?,?)", d_rows)
            m_rows.clear(), r_rows.clear(), d_rows.clear()

        for event, el in ET.iterparse(str(xml_path), events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                    build = el.get("build", "")
                continue
            if el.tag not in ("machine", "game"):
                continue
            name = el.get("name")
            default_bios = None
            first_bios = None
            has_disk = False
            for child in el:
                tag = child.tag
                if tag == "rom":
                    r_rows.append((name, child.get("name"), int(child.get("size") or 0),
                                   (child.get("crc") or "").lower() or None, child.get("merge"),
                                   child.get("status") or "good", 1 if child.get("optional") == "yes" else 0,
                                   child.get("bios")))
                    roms += 1
                elif tag == "device_ref":
                    d_rows.append((name, child.get("name")))
                    devices += 1
                elif tag == "biosset":
                    first_bios = first_bios or child.get("name")
                    if child.get("default") == "yes":
                        default_bios = child.get("name")
                elif tag == "disk":
                    has_disk = True
            m_rows.append((name, el.findtext("description"), el.findtext("year"), el.findtext("manufacturer"),
                           el.get("cloneof"), el.get("romof"), el.get("isbios") == "yes", el.get("isdevice") == "yes",
                           el.get("ismechanical") == "yes", el.get("runnable", "yes") != "no", has_disk,
                           default_bios or first_bios, el.get("sourcefile")))
            machines += 1
            el.clear()
            if root is not None:
                root.clear()  # drop processed machines from memory
            if len(m_rows) >= 2000:
                flush()
                if progress:
                    progress(machines)
        flush()
        c.execute("INSERT INTO meta VALUES ('build', ?)", (build,))
        c.execute("INSERT INTO meta VALUES ('source', ?)", (str(xml_path),))
        c.execute("INSERT INTO meta VALUES ('machines', ?)", (str(machines),))
    return {"machines": machines, "roms": roms, "device_refs": devices, "build": build}


# ---- lookups ----------------------------------------------------------------------

class Mame:
    def __init__(self, db: Database):
        self.db = db
        self._machines: dict[str, dict] | None = None
        self._roms: dict[str, list[dict]] = {}
        self._devs: dict[str, list[str]] = {}

    @property
    def loaded(self) -> bool:
        return bool(self.db.one("SELECT 1 FROM machines LIMIT 1"))

    def meta(self) -> dict:
        return {r["key"]: r["value"] for r in self.db.query("SELECT key, value FROM meta")}

    def machines(self) -> dict[str, dict]:
        if self._machines is None:
            self._machines = {r["name"]: dict(r) for r in self.db.query("SELECT * FROM machines")}
        return self._machines

    def machine(self, name: str) -> dict | None:
        ms = self.machines()
        return ms.get(name) or ms.get(name.lower())

    def roms(self, name: str) -> list[dict]:
        if name not in self._roms:
            self._roms[name] = [dict(r) for r in self.db.query("SELECT * FROM roms WHERE machine=?", (name,))]
        return self._roms[name]

    def device_refs(self, name: str) -> list[str]:
        if name not in self._devs:
            self._devs[name] = [r["device"] for r in self.db.query("SELECT device FROM device_refs WHERE machine=?", (name,))]
        return self._devs[name]

    def romof_chain(self, name: str) -> list[str]:
        chain, seen = [], {name}
        m = self.machine(name)
        while m and m.get("romof") and m["romof"] not in seen:
            seen.add(m["romof"])
            chain.append(m["romof"])
            m = self.machine(m["romof"])
        return chain

    def all_devices(self, name: str) -> list[str]:
        out, seen, stack = [], {name}, list(self.device_refs(name))
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            out.append(d)
            stack.extend(self.device_refs(d))
        return out

    def default_bios(self, name: str) -> str | None:
        for n in [name] + self.romof_chain(name):
            m = self.machine(n)
            if m and m.get("default_bios"):
                return m["default_bios"]
        return None

    def bios_of(self, name: str) -> str | None:
        for n in self.romof_chain(name):
            m = self.machine(n)
            if m and m["isbios"]:
                return n
        return None

    def classify(self, name: str) -> str:
        m = self.machine(name)
        if not m:
            return "unknown"
        if m["isbios"]:
            return "bios"
        if m["isdevice"] or not m["runnable"]:
            return "device"
        if m["ismechanical"]:
            return "mechanical"
        if m["cloneof"]:
            return "clone"
        return "game"

    def search(self, q: str, limit: int = 50) -> list[dict]:
        like = f"%{q}%"
        rows = self.db.query(
            "SELECT * FROM machines WHERE (description LIKE ? OR name LIKE ?) AND isdevice=0 "
            "ORDER BY (name = ?) DESC, cloneof IS NOT NULL, description LIMIT ?",
            (like, q + "%", q, limit),
        )
        return [dict(r) for r in rows]

    def supplier(self, machine: str, rom: dict) -> str:
        """The zip that nominally holds a ROM in a split set."""
        if not rom.get("merge"):
            return machine
        target_name = rom["merge"]
        for n in self.romof_chain(machine):
            for r in self.roms(n):
                if r["name"] == target_name or (rom.get("crc") and r.get("crc") == rom["crc"]):
                    if not r.get("merge"):
                        return n
                    target_name = r["merge"]
                    break
        chain = self.romof_chain(machine)
        return chain[-1] if chain else machine


# ---- verification -------------------------------------------------------------------

def zip_crcs(path: Path) -> set[str]:
    out = set()
    for e in archives.list_entries(path):
        if not e.is_dir and e.crc is not None:
            out.add(f"{e.crc:08x}")
    return out


class Have:
    """CRC sets of the zips in an arcade folder, loaded lazily."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.files: dict[str, Path] = {}
        if self.folder.is_dir():
            for e in os.scandir(self.folder):
                if e.is_file() and Path(e.name).suffix.lower() in archives.ARCHIVE_EXTS:
                    self.files.setdefault(Path(e.name).stem.casefold(), Path(e.path))
        self._crcs: dict[str, set[str]] = {}

    def has(self, name: str) -> bool:
        return name.casefold() in self.files

    def crcs(self, name: str) -> set[str]:
        key = name.casefold()
        if key not in self._crcs:
            p = self.files.get(key)
            try:
                self._crcs[key] = zip_crcs(p) if p else set()
            except Exception:  # noqa: BLE001 - unreadable zip counts as empty
                self._crcs[key] = set()
        return self._crcs[key]


@dataclass
class SetResult:
    name: str
    status: str            # good | missing | unknown
    cls: str               # game | clone | bios | device | mechanical | unknown
    description: str = ""
    missing: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)   # other zips this set depends on


def verify_set(mame: Mame, have: Have, name: str) -> SetResult:
    fname, name = name, name.lower()  # MAME short names are lower case; keep the file's spelling for display
    m = mame.machine(name)
    if not m:
        return SetResult(fname, "unknown", "unknown", notes=["not in the MAME reference data"])
    res = SetResult(fname, "good", mame.classify(name), m["description"] or "")
    chain = [name] + mame.romof_chain(name)
    default_bios = mame.default_bios(name)
    nodump = 0

    def check(rom: dict, owner: str, search: list[str]):
        nonlocal nodump
        if rom["status"] == "nodump" or not rom["crc"]:
            nodump += 1
            return
        if rom["optional"]:
            return
        if rom.get("bios") and default_bios and rom["bios"] != default_bios:
            return  # alternative BIOS version, not the default one
        if any(rom["crc"] in have.crcs(z) for z in search):
            return
        sup = owner if owner != name else mame.supplier(name, rom)
        res.missing.append({"rom": rom["name"], "crc": rom["crc"], "size": rom["size"],
                            "supplier": sup, "supplier_present": have.has(sup)})

    for rom in mame.roms(name):
        check(rom, name, chain)
    for dev in mame.all_devices(name):
        dev_roms = mame.roms(dev)
        if dev_roms:
            res.needs.append(dev)
            for rom in dev_roms:
                check(rom, dev, [dev] + chain)
    res.needs = sorted(set(res.needs) | set(chain[1:]))
    if nodump:
        res.notes.append(f"{nodump} ROM(s) with no good dump known (informational)")
    if m["has_disk"]:
        res.notes.append("also needs a CHD disk image (not checked)")
    if res.missing:
        res.status = "missing"
    return res


def verify_folder(mame: Mame, folder: Path, progress=None) -> list[SetResult]:
    have = Have(folder)
    out = []
    for i, key in enumerate(sorted(have.files)):
        name = have.files[key].stem
        out.append(verify_set(mame, have, name))
        if progress and i % 200 == 0:
            progress(i)
    return out
