"""BIOS catalog, MD5 scanning, status, and install (mounted or export bundle)."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import yaml
from pydantic import BaseModel, Field, field_validator

from . import archives, fsutil

INSTALL_DIR = "_install"
SCRIPT_NAME = "install-bios.ps1"


class BiosEntry(BaseModel):
    system: str
    file: str
    md5: str = ""
    required: bool = False
    note: str = ""

    @field_validator("md5")
    @classmethod
    def _lower(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and (len(v) != 32 or any(c not in "0123456789abcdef" for c in v)):
            raise ValueError(f"not an MD5: {v}")
        return v

    @field_validator("file")
    @classmethod
    def _rel(cls, v: str) -> str:
        v = v.replace("\\", "/").strip("/")
        if not v or ".." in v.split("/"):
            raise ValueError(f"bad BIOS file path: {v}")
        return v

    @property
    def key(self) -> tuple[str, str]:
        return (self.system, self.file.casefold())

    @property
    def basename(self) -> str:
        return self.file.rsplit("/", 1)[-1]


class CatalogFile(BaseModel):
    bios: list[BiosEntry] = Field(default_factory=list)


def parse_catalog(text: str) -> list[BiosEntry]:
    return CatalogFile.model_validate(yaml.safe_load(text) or {}).bios


def load_catalog(path: Path) -> list[BiosEntry]:
    return parse_catalog(Path(path).read_text(encoding="utf-8"))


# ---- matching ----------------------------------------------------------------

@dataclass
class Match:
    entry: BiosEntry
    how: str  # md5 | name (entry has no hash) | name-mismatch (same name, different hash)

    @property
    def satisfies(self) -> bool:
        return self.how in ("md5", "name")


def match_file(name: str, md5: str, catalog: list[BiosEntry]) -> list[Match]:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    out = []
    for e in catalog:
        if e.md5 and e.md5 == md5:
            out.append(Match(e, "md5"))
        elif e.basename.casefold() == base:
            out.append(Match(e, "name" if not e.md5 else "name-mismatch"))
    return out


@dataclass
class StagedFile:
    path: Path
    rel: str
    size: int
    md5: str
    matches: list[Match] = field(default_factory=list)


class Md5Cache:
    """MD5s keyed by (path, size, mtime) in the app DB so rescans are cheap."""

    SCHEMA = "CREATE TABLE IF NOT EXISTS md5_cache (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, md5 TEXT)"

    def __init__(self, db):
        self.db = db
        db.execute(self.SCHEMA)

    def get(self, path: Path) -> str:
        st = path.stat()
        row = self.db.one("SELECT size, mtime, md5 FROM md5_cache WHERE path=?", (str(path),))
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime_ns:
            return row["md5"]
        md5 = fsutil.md5_file(path)
        self.db.execute("INSERT OR REPLACE INTO md5_cache(path, size, mtime, md5) VALUES (?,?,?,?)",
                        (str(path), st.st_size, st.st_mtime_ns, md5))
        return md5


def scan_staged(bios_dir: Path, catalog: list[BiosEntry], cache: Md5Cache) -> list[StagedFile]:
    out = []
    if not bios_dir.is_dir():
        return out
    for root, dirs, files in os.walk(bios_dir):
        dirs[:] = [d for d in dirs if d != INSTALL_DIR and not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = Path(root) / f
            md5 = cache.get(p)
            out.append(StagedFile(p, str(p.relative_to(bios_dir)).replace("\\", "/"), p.stat().st_size, md5,
                                  match_file(f, md5, catalog)))
    return sorted(out, key=lambda s: s.rel.casefold())


@dataclass
class ArchiveMember:
    name: str
    size: int
    md5: str
    matches: list[Match]


def scan_archive(path: Path, catalog: list[BiosEntry], max_member: int = 256 * 1024 * 1024) -> list[ArchiveMember]:
    """MD5 every member of a zip/7z (BIOS sets are small) and match against the catalog."""
    path = Path(path)
    out = []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.file_size > max_member:
                    continue
                with zf.open(info) as fh:
                    md5 = fsutil.md5_stream(fh)
                out.append(ArchiveMember(info.filename, info.file_size, md5, match_file(info.filename, md5, catalog)))
        return out
    tmp = Path(tempfile.mkdtemp(prefix="bios-scan-"))
    try:
        archives.extract_all(path, tmp)
        for root, _d, files in os.walk(tmp):
            for f in files:
                p = Path(root) / f
                rel = str(p.relative_to(tmp)).replace("\\", "/")
                md5 = fsutil.md5_file(p)
                out.append(ArchiveMember(rel, p.stat().st_size, md5, match_file(rel, md5, catalog)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return sorted(out, key=lambda m: m.name.casefold())


# ---- status / install plan --------------------------------------------------------

@dataclass
class EntryStatus:
    entry: BiosEntry
    sources: list[StagedFile]
    installed: str  # ok | mismatch | missing | unknown (target not mounted)
    action: str = ""  # install | installed | missing-source | conflict | n/a
    source: StagedFile | None = None


def installed_state(target_root: Path | None, e: BiosEntry, cache: Md5Cache | None = None) -> str:
    if target_root is None:
        return "unknown"
    t = target_root / e.file
    if not t.is_file():
        return "missing"
    if not e.md5:
        return "ok"
    md5 = cache.get(t) if cache else fsutil.md5_file(t)
    return "ok" if md5 == e.md5 else "mismatch"


def mounted_root(cfg) -> Path | None:
    p = cfg.paths.desktop_bios
    if p and Path(p).is_dir():
        return Path(p)
    return None


def build_status(catalog: list[BiosEntry], staged: list[StagedFile], target_root: Path | None,
                 cache: Md5Cache | None = None) -> list[EntryStatus]:
    rows = []
    for e in catalog:
        sources = [s for s in staged if any(m.entry.key == e.key and m.satisfies for m in s.matches)]
        # prefer an exact-hash source, then a file already named like the target
        sources.sort(key=lambda s: (not any(m.entry.key == e.key and m.how == "md5" for m in s.matches),
                                    s.path.name.casefold() != e.basename.casefold()))
        inst = installed_state(target_root, e, cache)
        if inst == "ok":
            action = "installed"
        elif not sources:
            action = "missing-source"
        elif inst == "mismatch":
            action = "conflict"
        else:
            action = "install"
        rows.append(EntryStatus(e, sources, inst, action, sources[0] if sources else None))
    return rows


def system_summary(rows: list[EntryStatus]) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows:
        d = by.setdefault(r.entry.system, {"system": r.entry.system, "req_total": 0, "req_staged": 0,
                                           "req_installed": 0, "opt_total": 0, "opt_staged": 0,
                                           "opt_installed": 0, "missing_required": []})
        pre = "req" if r.entry.required else "opt"
        d[f"{pre}_total"] += 1
        if r.sources:
            d[f"{pre}_staged"] += 1
        if r.installed == "ok":
            d[f"{pre}_installed"] += 1
        if r.entry.required and not r.sources and r.installed != "ok":
            d["missing_required"].append(r.entry.file)
    return sorted(by.values(), key=lambda d: d["system"])


# ---- export bundle + PowerShell ------------------------------------------------------

def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def powershell_script(share_unc: str, desktop_path: str, entries: list[BiosEntry]) -> str:
    src = str(PureWindowsPath(share_unc.rstrip("\\/")) / "_bios" / INSTALL_DIR)
    lines = [
        "# Retro Library Manager: copy staged BIOS files into RetroBat. Never overwrites.",
        f"$src = {_ps(src)}",
        f"$dst = {_ps(desktop_path)}",
        "$files = @(",
    ]
    for i, e in enumerate(entries):
        sep = "," if i < len(entries) - 1 else ""
        lines.append(f"  @({_ps(e.file.replace('/', chr(92)))}, {_ps(e.md5)}){sep}")
    lines += [
        ")",
        "foreach ($f in $files) {",
        "  $s = Join-Path $src $f[0]; $t = Join-Path $dst $f[0]",
        "  if (Test-Path -LiteralPath $t) { $state = 'exists' }",
        "  else { New-Item -ItemType Directory -Force -Path (Split-Path $t) | Out-Null; Copy-Item -LiteralPath $s -Destination $t; $state = 'copied' }",
        "  $h = (Get-FileHash -LiteralPath $t -Algorithm MD5).Hash.ToLower()",
        "  $ok = if ($f[1] -eq '' -or $h -eq $f[1]) { 'OK' } else { 'MD5-MISMATCH' }",
        "  '{0,-7} {1,-12} {2}  {3}' -f $state, $ok, $h, $f[0]",
        "}",
    ]
    return "\n".join(lines) + "\n"


# ---- catalog import (RetroBat definition file) -----------------------------------------

def parse_definition(text: str) -> list[BiosEntry]:
    """Best-effort parser for BIOS definition files.

    Accepts our own YAML format, or XML where elements carrying an ``md5``
    attribute name a file via ``path``/``file``/``name`` and inherit the system
    from the nearest ancestor with a ``name``/``system``/``id`` attribute
    (the layout used by EmulationStation-style es_bios.xml files).
    """
    text = text.lstrip("﻿")
    if not text.lstrip().startswith("<"):
        return parse_catalog(text)
    root = ET.fromstring(text)
    out: list[BiosEntry] = []

    def walk(el, system: str | None):
        md5 = el.get("md5")
        if md5 is None:
            system = el.get("system") or el.get("name") or el.get("id") or system
        else:
            fname = (el.get("path") or el.get("file") or el.get("name") or "").replace("\\", "/")
            if fname.lower().startswith("bios/"):
                fname = fname[5:]
            required = ((el.get("required") or "").lower() in ("1", "true", "yes")
                        or (el.get("optional") or "").lower() in ("0", "false", "no"))
            if fname:
                try:
                    out.append(BiosEntry(system=el.get("system") or system or "unknown", file=fname,
                                         md5=md5.split(",")[0].strip(), required=required,
                                         note=el.get("description") or el.get("note") or ""))
                except ValueError:
                    pass  # malformed hash or path: skip the entry
        for child in el:
            walk(child, system)

    walk(root, None)
    return out


def merge_new(catalog: list[BiosEntry], incoming: list[BiosEntry]) -> list[BiosEntry]:
    """Entries from ``incoming`` whose (system, file) is not already catalogued."""
    have = {e.key for e in catalog}
    new, seen = [], set()
    for e in incoming:
        if e.key in have or e.key in seen:
            continue
        seen.add(e.key)
        new.append(e)
    return new


def append_to_catalog_file(path: Path, entries: list[BiosEntry]) -> str:
    """Append entries to bios_catalog.yaml text, preserving the existing file verbatim."""
    text = Path(path).read_text(encoding="utf-8").rstrip("\n") + "\n"
    if not entries:
        return text
    buf = io.StringIO()
    buf.write("  # --- imported ---\n")
    for e in entries:
        d = {"system": e.system, "file": e.file, "md5": e.md5, "required": e.required, "note": e.note}
        line = yaml.safe_dump([d], default_flow_style=None, sort_keys=False, width=4096, allow_unicode=True)
        buf.write("  " + line.strip() + "\n")
    if "bios: []" in text:
        text = text.replace("bios: []", "bios:")
    return text + buf.getvalue()


# ---- jobs ---------------------------------------------------------------------------------

def _catalog_and_staged(state):
    catalog = load_catalog(state.settings.bios_catalog_path)
    cache = Md5Cache(state.db)
    staged = scan_staged(state.cfg.bios_dir, catalog, cache)
    return catalog, cache, staged


def run_install_mounted(state, ctx) -> dict:
    cfg = state.cfg
    root = mounted_root(cfg)
    if root is None:
        raise RuntimeError(f"desktop BIOS folder is not mounted at '{cfg.paths.desktop_bios}'")
    catalog, cache, staged = _catalog_and_staged(state)
    rows = build_status(catalog, staged, root)
    wanted = set(ctx.params.get("files") or [])
    for r in rows:
        ctx.check()
        tag = f"{r.entry.system}/{r.entry.file}"
        if wanted and tag not in wanted:
            continue
        if r.action != "install":
            ctx.count(r.action)
            ctx.item(r.entry.file, r.action, r.entry.system)
            continue
        target = root / r.entry.file
        fsutil.ensure_dir(target.parent)
        try:
            fsutil.copy_new(r.source.path, target, ctx.check)
        except FileExistsError:
            ctx.count("conflict")
            ctx.item(r.entry.file, "skipped", "target appeared meanwhile; not overwritten")
            continue
        md5 = fsutil.md5_file(target)
        expected = r.entry.md5 or r.source.md5
        if md5 != expected:
            target.unlink()  # our own just-written copy
            ctx.count("failed")
            ctx.item(r.entry.file, "failed", f"MD5 after copy {md5} != {expected}; copy removed")
            continue
        ctx.count("installed")
        ctx.item(r.entry.file, "installed", f"{r.entry.system}: from {r.source.rel}, MD5 verified {md5}")
        ctx.log(f"INSTALLED {r.entry.file} (md5 {md5})")
    return {}


def run_export(state, ctx) -> dict:
    cfg = state.cfg
    catalog, cache, staged = _catalog_and_staged(state)
    rows = build_status(catalog, staged, None)
    bundle = fsutil.ensure_dir(cfg.bios_dir / INSTALL_DIR)
    included: list[BiosEntry] = []
    for r in rows:
        ctx.check()
        if not r.source:
            continue
        dest = bundle / r.entry.file
        fsutil.ensure_dir(dest.parent)
        if dest.exists():
            md5 = fsutil.md5_file(dest)
            if md5 == (r.entry.md5 or r.source.md5):
                ctx.count("already_bundled")
                included.append(r.entry)
            else:
                ctx.count("conflict")
                ctx.item(r.entry.file, "skipped", "a different file is already in the bundle; not overwritten")
            continue
        fsutil.copy_verify(r.source.path, dest, ctx.check)
        ctx.count("bundled")
        ctx.item(r.entry.file, "added", f"{r.entry.system}: from {r.source.rel}")
        ctx.log(f"BUNDLED {r.entry.file}")
        included.append(r.entry)
    script = powershell_script(cfg.bios.share_unc, cfg.bios.desktop_path, included)
    script_path = bundle / SCRIPT_NAME
    tmp = bundle / f".{SCRIPT_NAME}.tmp"
    tmp.write_text(script, encoding="utf-8")
    os.replace(tmp, script_path)  # generated file, safe to refresh
    fsutil.fix_perms(bundle, recursive=True)
    ctx.log("PowerShell to run on the desktop:\n" + script)
    return {"script": str(script_path)}


def run_stage_from_archive(state, ctx) -> dict:
    """Copy catalog-matching members of an archive into _bios/<system>/ (original names)."""
    cfg = state.cfg
    catalog = load_catalog(state.settings.bios_catalog_path)
    path = Path(ctx.params["archive"])
    ready = fsutil.wait_stable(path, cfg.stability.wait_seconds)
    if not ready.ok:
        raise RuntimeError(f"{path.name} is not ready: {ready.reason}")
    members = scan_archive(path, catalog)
    tmp = None
    try:
        for m in members:
            sats = [x for x in m.matches if x.satisfies]
            if not sats:
                continue
            ctx.check()
            systems = sorted({x.entry.system for x in sats})
            for system in systems:
                dest = fsutil.ensure_dir(cfg.bios_dir / system) / m.name.rsplit("/", 1)[-1]
                if dest.exists():
                    ctx.count("present")
                    ctx.item(dest.name, "present", f"_bios/{system}/ already has it")
                    continue
                if path.suffix.lower() == ".zip":
                    archives.extract_zip_member(path, m.name, dest)
                else:
                    if tmp is None:
                        tmp = Path(tempfile.mkdtemp(prefix="bios-", dir=fsutil.ensure_dir(cfg.temp_dir)))
                        archives.extract_all(path, tmp)
                    fsutil.copy_new(tmp / m.name, dest)
                ctx.count("staged")
                ctx.item(dest.name, "added", f"_bios/{system}/")
                ctx.log(f"STAGED {m.name} -> _bios/{system}/")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    fsutil.fix_perms(cfg.bios_dir, recursive=True)
    return {}
