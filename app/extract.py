"""Import and extraction engine.

``plan()`` is the dry run: it decides every archive entry without writing
anything. ``run()`` (a job handler) re-plans against the current state of the
disk and then performs exactly the "include" and "bios" rows of that plan.
Re-runs are cheap: anything whose stem already exists is "present".
"""
from __future__ import annotations

import csv
import io
import os
import posixpath
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import archives, chd, fsutil
from .config import Config
from .filters import BIOS, EXCLUDE, INCLUDE, Decision, RuleSet
from .hooks import HOOKS
from .systems import ARCHIVE_EXTS, System

# Decisions shown in the dry run
PRESENT = "present"
SKIPPED = "skipped"

PART = "part"  # a track file planned together with its .cue/.gdi

# Entry kinds
ROM, INNER, SET, OTHER, DISC, TRACK = "rom", "inner", "set", "other", "disc", "track"


@dataclass
class Row:
    entry: str
    base: str
    kind: str
    decision: str
    rule: str
    target: str = ""
    members: list[str] = field(default_factory=list)  # DISC rows: the track entries that go with it


@dataclass
class Plan:
    archive: str
    system: str
    rows: list[Row] = field(default_factory=list)

    @property
    def totals(self) -> Counter:
        return Counter(r.decision for r in self.rows)

    def by_decision(self, decision: str) -> list[Row]:
        return [r for r in self.rows if r.decision == decision]

    def csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["entry", "base", "kind", "decision", "rule", "target"])
        for r in self.rows:
            w.writerow([r.entry, r.base, r.kind, r.decision, r.rule, r.target])
        return buf.getvalue()


def existing_stems(folder: Path) -> set[str]:
    """Case-folded stems of files already in a system folder (dedupe key)."""
    if not folder.is_dir():
        return set()
    return {Path(e.name).stem.casefold() for e in os.scandir(folder) if e.is_file() and not e.name.startswith(".")}


def existing_names(folder: Path) -> set[str]:
    if not folder.is_dir():
        return set()
    return {e.name.casefold() for e in os.scandir(folder) if e.is_file()}


def _parent(name: str) -> str:
    return name.rsplit("/", 1)[0] if "/" in name else ""


def disc_groups(source: Path, entries: list[archives.Entry]) -> tuple[dict, dict, set]:
    """Group .cue/.gdi sheets with the track files they reference.

    Returns (groups, owner, odd_layouts):
      groups[descriptor] = (member entry names, missing reference names)
      owner[track entry] = descriptor entry name
      odd_layouts = {(folder, stem)} of CloneCD/MDS/NRG sets, which chdman cannot read
    """
    files = [e for e in entries if not e.is_dir]
    by_path = {e.name.casefold(): e.name for e in files}
    by_dir_base = {(_parent(e.name).casefold(), e.base.casefold()): e.name for e in files}
    groups: dict[str, tuple[list[str], list[str]]] = {}
    owner: dict[str, str] = {}
    odd = {(_parent(e.name).casefold(), e.stem.casefold()) for e in files if e.ext in (".ccd", ".mds", ".nrg")}
    for e in files:
        if e.ext not in (".cue", ".gdi"):
            continue
        try:
            text = archives.read_member(source, e.name, 1024 * 1024).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - unreadable sheet: treat every reference as missing
            text = ""
        d = _parent(e.name)
        members, missing = [], []
        for ref in chd.descriptor_refs(text, e.ext):
            ref = ref.replace("\\", "/")
            joined = posixpath.normpath(f"{d}/{ref}" if d else ref)
            hit = by_path.get(joined.casefold()) or by_dir_base.get((d.casefold(), ref.rsplit("/", 1)[-1].casefold()))
            if hit and hit not in members:
                members.append(hit)
                owner.setdefault(hit, e.name)
            elif not hit:
                missing.append(ref)
        if not text:
            missing = ["(could not read the sheet)"]
        groups[e.name] = (members, missing)
    return groups, owner, odd


def plan(cfg: Config, sysdef: System, archive: Path, entries: list[archives.Entry] | None = None,
         select: set[str] | None = None, override: bool = True) -> Plan:
    """Decide every entry without writing anything.

    ``select`` limits the plan to those entries. With ``override`` (the default)
    the operator's choice also overrides the region/quality filter; never
    "present" (no overwrites) or BIOS staging. For disc systems a .cue/.gdi and
    its tracks are planned as one unit: selecting either brings the whole set.
    """
    archive = Path(archive)
    entries = entries if entries is not None else archives.list_entries(archive)
    disc_mode = bool(sysdef.disc) and not sysdef.arcade
    groups, owner, odd = disc_groups(archive, entries) if disc_mode else ({}, {}, set())
    if select is not None:
        select = set(select)
        for desc, (members, _missing) in groups.items():
            if desc in select or any(m in select for m in members):
                select.add(desc)
                select.update(members)
        entries = [e for e in entries if e.name in select]

    def manual(d: Decision) -> Decision:
        if select is not None and override and d.category == EXCLUDE:
            return Decision(INCLUDE, f"selected manually (filter: {d.rule})")
        return d

    rules = RuleSet(cfg.filters, sysdef.key)
    folder = cfg.system_dir(sysdef.key)
    bios_folder = cfg.bios_dir / sysdef.key
    stems = existing_stems(folder)
    names = existing_names(folder)
    bios_names = existing_names(bios_folder)
    bios_stems = existing_stems(bios_folder)
    ignore = {e.lower() for e in cfg.ignore_extensions}
    accepted = sysdef.rom_extensions - set(ARCHIVE_EXTS)
    seen: set[str] = set()
    p = Plan(str(archive), sysdef.key)

    for e in entries:
        if e.is_dir:
            continue
        base, ext, stem = e.base, e.ext, e.stem
        if ext in ignore:
            p.rows.append(Row(e.name, base, OTHER, SKIPPED, f"ignored file type {ext}"))
            continue
        bios_why = rules.bios_rule(base)
        if bios_why:
            # A per-game archive holding firmware is unpacked into _bios (never for arcade sets).
            inner = ext in ARCHIVE_EXTS and not sysdef.arcade
            kind = INNER if inner else ROM
            target = f"_bios/{sysdef.key}/{stem}.*" if inner else f"_bios/{sysdef.key}/{base}"
            staged = stem.casefold() in bios_stems if inner else base.casefold() in bios_names
            if staged:
                p.rows.append(Row(e.name, base, kind, PRESENT, "BIOS already staged", target))
            elif "bios:" + base.casefold() in seen:
                p.rows.append(Row(e.name, base, kind, SKIPPED, "duplicate name in archive"))
            else:
                seen.add("bios:" + base.casefold())
                p.rows.append(Row(e.name, base, kind, BIOS, bios_why, target))
            continue

        if sysdef.arcade:
            if ext not in ARCHIVE_EXTS:
                p.rows.append(Row(e.name, base, OTHER, SKIPPED, f"not a set archive ({ext or 'no extension'})"))
                continue
            d = manual(rules.decide_arcade(e.name))
            key = base.casefold()
            if d.category != INCLUDE:
                p.rows.append(Row(e.name, base, SET, d.category, d.rule))
            elif key in names:
                p.rows.append(Row(e.name, base, SET, PRESENT, "set already in folder", base))
            elif key in seen:
                p.rows.append(Row(e.name, base, SET, SKIPPED, "duplicate name in archive"))
            else:
                seen.add(key)
                p.rows.append(Row(e.name, base, SET, INCLUDE, d.rule, base))
            continue

        if disc_mode:
            if e.name in owner:
                continue  # planned together with its .cue/.gdi
            if (_parent(e.name).casefold(), stem.casefold()) in odd:
                p.rows.append(Row(e.name, base, OTHER, SKIPPED,
                                  "CloneCD/MDS/NRG layout: chdman needs .cue/.bin, convert it first"))
                continue
            if e.name in groups:
                p.rows.extend(_plan_disc_set(e, groups[e.name], manual(rules.decide(e.name)), stems, names, seen))
                continue

        kind = INNER if ext in ARCHIVE_EXTS else ROM
        if kind == ROM and accepted and ext not in accepted:
            p.rows.append(Row(e.name, base, OTHER, SKIPPED, f"extension {ext or '(none)'} not accepted by {sysdef.key}"))
            continue
        if disc_mode and ext == ".iso":
            kind = DISC  # a single-file disc image: convertible to CHD
        d = manual(rules.decide(e.name))
        key = stem.casefold()
        # Inside one archive, "Game.cue" and "Game.bin" are distinct files; only
        # identical base names are duplicates. Against the folder, the stem decides.
        dup_key = key if kind == INNER else base.casefold()
        target = base if kind == ROM else (f"{stem}.chd / {base}" if kind == DISC else f"{stem}.*")
        if d.category == BIOS:  # custom bios prefix handled above; keep for completeness
            p.rows.append(Row(e.name, base, kind, BIOS, d.rule, f"_bios/{sysdef.key}/{base}"))
        elif d.category != INCLUDE:
            p.rows.append(Row(e.name, base, kind, EXCLUDE, d.rule))
        elif key in stems:
            p.rows.append(Row(e.name, base, kind, PRESENT, f"{stem}.* already exists"))
        elif dup_key in seen:
            p.rows.append(Row(e.name, base, kind, SKIPPED, "duplicate name in archive"))
        else:
            seen.add(dup_key)
            p.rows.append(Row(e.name, base, kind, INCLUDE, d.rule, target))
    return p


def _plan_disc_set(e: archives.Entry, group, d: Decision, stems: set[str], names: set[str],
                   seen: set[str]) -> list[Row]:
    members, missing = group
    base, stem = e.base, e.stem
    n = len(members)
    tracks = f"{n} track file{'s' if n != 1 else ''}"
    if d.category != INCLUDE:
        row = Row(e.name, base, DISC, EXCLUDE, d.rule, members=members)
    elif not members and not missing:
        row = Row(e.name, base, DISC, SKIPPED, "the sheet references no track files")
    elif missing:
        row = Row(e.name, base, DISC, SKIPPED, f"incomplete set: missing {', '.join(missing[:5])}",
                  members=members)
    elif stem.casefold() in stems:
        row = Row(e.name, base, DISC, PRESENT, f"{stem}.* already exists", members=members)
    elif any(m.rsplit("/", 1)[-1].casefold() in names for m in members):
        row = Row(e.name, base, DISC, SKIPPED, "some of its tracks are already in the folder; not mixing sets",
                  members=members)
    elif base.casefold() in seen:
        row = Row(e.name, base, DISC, SKIPPED, "duplicate name in archive", members=members)
    else:
        seen.add(base.casefold())
        row = Row(e.name, base, DISC, INCLUDE, f"{d.rule} · disc set with {tracks}",
                  f"{stem}.chd / {base} + {tracks}", members=members)
    parts = [Row(m, m.rsplit("/", 1)[-1], TRACK, PART, f"track of {base} (goes with it)") for m in members]
    return [row] + parts


# ---- execution -----------------------------------------------------------------

class Extractor:
    """Pulls single members out of the outer archive (zip directly, 7z via a temp tree)."""

    def __init__(self, archive: Path, tmp_root: Path):
        self.archive = Path(archive)
        self.is_dir = self.archive.is_dir()
        self.is_zip = not self.is_dir and self.archive.suffix.lower() == ".zip"
        self.tmp_root = tmp_root
        self._tree: Path | None = None

    def write_member(self, member: str, dest: Path, check=None) -> int:
        if self.is_dir:
            src = self.archive / member
            if not fsutil.is_within(src, self.archive):
                raise ValueError(f"unsafe path: {member}")
            return fsutil.copy_new(src, dest, check)
        if self.is_zip:
            return archives.extract_zip_member(self.archive, member, dest, check)
        if self._tree is None:
            self._tree = Path(tempfile.mkdtemp(prefix="outer-", dir=self.tmp_root))
            archives.extract_all(self.archive, self._tree)
        return fsutil.copy_new(self._tree / member, dest, check)

    def member_to_temp(self, member: str, check=None) -> Path:
        d = Path(tempfile.mkdtemp(prefix="inner-", dir=self.tmp_root))
        dest = d / Path(member).name
        self.write_member(member, dest, check)
        return dest

    def close(self) -> None:
        if self._tree is not None:
            shutil.rmtree(self._tree, ignore_errors=True)


def _copy_files_all_or_nothing(ctx, files: list[tuple[Path, str]], folder: Path, label: str) -> bool:
    """Copy (source path, name) pairs into ``folder`` as one unit; nothing is left half-copied."""
    if any((folder / name).exists() for _src, name in files):
        ctx.count("present")
        ctx.item(label, "present", "already in the folder (whole set or part of it); not overwritten")
        return False
    written: list[Path] = []
    try:
        for src, name in files:
            fsutil.copy_new(src, folder / name, ctx.check)
            written.append(folder / name)
    except BaseException:
        for w in written:
            w.unlink(missing_ok=True)
        raise
    return True


def _unpack_inner_disc(ctx, sysdef: System, work: Path, folder: Path, row: Row) -> None:
    """A per-game disc archive: copy each .cue/.gdi with all of its tracks, or nothing."""
    names = [str(Path(r) / f) for r, _d, fs in os.walk(work) for f in fs]
    descs, problem = chd.pick_descriptors(names)
    if problem:
        ctx.count("skipped")
        ctx.item(row.base, "skipped", problem)
        ctx.log(f"SKIP {row.base}: {problem}")
        return
    for desc in map(Path, descs):
        tracks, missing = chd.resolve_tracks(desc)
        if missing:
            ctx.count("failed")
            ctx.item(desc.name, "failed", f"from {row.base}: missing track(s) {', '.join(missing)}; nothing copied")
            continue
        if any(t.parent != desc.parent for t in tracks):
            ctx.count("failed")
            ctx.item(desc.name, "failed", f"from {row.base}: tracks are in sub-folders; convert to CHD instead")
            continue
        files = [(desc, desc.name)] + [(t, t.name) for t in tracks]
        if _copy_files_all_or_nothing(ctx, files, folder, desc.name):
            existing_stems_cache(ctx, folder).add(desc.stem.casefold())
            ctx.count("added")
            ctx.item(desc.name, "added", f"from {row.base} with {len(tracks)} track file(s)")
            ctx.log(f"ADD  {desc.name} + {len(tracks)} track(s)  (from {row.base})")


def _unpack_inner(ctx, cfg: Config, sysdef: System, inner_archive: Path, folder: Path, row: Row) -> None:
    """Unpack one per-game inner archive's ROM files flat into the system folder."""
    work = inner_archive.parent / "x"
    archives.extract_all(inner_archive, work)
    if sysdef.disc and any(Path(f).suffix.lower() in chd.DISC_EXTS for _r, _d, fs in os.walk(work) for f in fs):
        _unpack_inner_disc(ctx, sysdef, work, folder, row)
        return
    ignore = {e.lower() for e in cfg.ignore_extensions}
    accepted = sysdef.rom_extensions - set(ARCHIVE_EXTS)
    roms = []
    for root, _d, files in os.walk(work):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in ignore:
                continue
            if accepted and ext not in accepted:
                ctx.log(f"  {row.base}: ignoring {f} (extension not accepted)")
                continue
            roms.append(Path(root) / f)
    if not roms:
        ctx.count("skipped")
        ctx.item(row.base, "skipped", "inner archive has no ROM files for this system")
        ctx.log(f"SKIP {row.base}: no ROM files inside")
        return
    before = set(existing_stems_cache(ctx, folder))  # siblings from this archive may share a stem
    for src in roms:
        dest = folder / src.name
        if dest.exists() or Path(src.name).stem.casefold() in before:
            ctx.count("present")
            ctx.item(src.name, "present", f"from {row.base}: already exists")
            continue
        fsutil.copy_new(src, dest, ctx.check)
        existing_stems_cache(ctx, folder).add(Path(src.name).stem.casefold())
        ctx.count("added")
        ctx.item(src.name, "added", f"from {row.base}")
        ctx.log(f"ADD  {src.name}  (from {row.base})")


def _stage_inner_bios(ctx, cfg: Config, sysdef: System, inner_archive: Path, bios_folder: Path, row: Row) -> None:
    """Unpack a per-game firmware archive into _bios/<system>/ (original names, no overwrite)."""
    work = inner_archive.parent / "x"
    archives.extract_all(inner_archive, work)
    ignore = {e.lower() for e in cfg.ignore_extensions}
    found = False
    for root, _d, files in os.walk(work):
        for f in sorted(files):
            if Path(f).suffix.lower() in ignore:
                continue
            found = True
            dest = bios_folder / f
            if dest.exists():
                ctx.count("present")
                ctx.item(f, "present", f"from {row.base}: already staged")
                continue
            fsutil.copy_new(Path(root) / f, dest, ctx.check)
            ctx.count("bios_staged")
            ctx.item(f, "bios", f"from {row.base}, staged to _bios/{sysdef.key}/")
            ctx.log(f"BIOS {f} (from {row.base}) -> _bios/{sysdef.key}/")
    if not found:
        ctx.count("skipped")
        ctx.item(row.base, "skipped", "archive contains no files")


def _add_disc_set(ctx, cfg: Config, sysdef: System, ex: "Extractor", row: Row, folder: Path,
                  tmp_root: Path, convert: bool) -> None:
    """A .cue/.gdi (with its tracks) or a single .iso from the outer archive."""
    desc_dir = _parent(row.entry)
    rels = [(m, posixpath.relpath(m, desc_dir) if desc_dir else m) for m in [row.entry] + row.members]
    if convert:
        work = Path(tempfile.mkdtemp(prefix="disc-", dir=tmp_root))
        try:
            for member, rel in rels:
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                ex.write_member(member, dest, ctx.check)
            stem = Path(row.base).stem
            game = chd.Game(stem, "loose", work / rels[0][1], targets=[stem])
            chd.convert_game(ctx, cfg, sysdef, game, backup=False)  # the source is the backup
            existing_stems_cache(ctx, folder).add(stem.casefold())
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return
    if any("/" in rel or rel.startswith("..") for _m, rel in rels[1:]):
        ctx.count("failed")
        ctx.item(row.base, "failed", "tracks are in sub-folders; flat copy would break the sheet; convert to CHD instead")
        return
    if any((folder / posixpath.basename(m)).exists() for m, _rel in rels):
        ctx.count("present")
        ctx.item(row.base, "present", "already in the folder (whole set or part of it); not overwritten")
        return
    written: list[Path] = []
    try:
        for member, _rel in rels:
            dest = folder / posixpath.basename(member)
            ex.write_member(member, dest, ctx.check)
            written.append(dest)
    except BaseException:
        for w in written:
            w.unlink(missing_ok=True)
        raise
    existing_stems_cache(ctx, folder).add(Path(row.base).stem.casefold())
    ctx.count("added")
    extra = f" with {len(row.members)} track file(s)" if row.members else ""
    ctx.item(row.base, "added", row.rule + extra)
    ctx.log(f"ADD  {row.base}{extra}")


def existing_stems_cache(ctx, folder: Path) -> set[str]:
    cache = getattr(ctx, "_stems", None)
    if cache is None:
        cache = existing_stems(folder)
        ctx._stems = cache
    return cache


def execute(ctx, cfg: Config, sysdef: System, archive: Path, select: list[str] | None = None,
            convert: bool | None = None, override: bool = True) -> dict:
    """Extract the plan. ``convert``: disc games become CHDs (default: the system's ``prefer``)."""
    if convert is None:
        convert = sysdef.prefers_chd
    convert = convert and bool(sysdef.disc)
    archive = Path(archive)
    ready = fsutil.wait_stable(archive, cfg.stability.wait_seconds)
    if not ready.ok:
        raise RuntimeError(f"{archive.name} is not ready: {ready.reason}")
    ctx.log(f"{archive} is stable; planning against {cfg.system_dir(sysdef.key)}")
    if sysdef.disc:
        ctx.log("Disc games: " + ("convert to CHD" if convert else "copy original files"))
    p = plan(cfg, sysdef, archive, select=set(select) if select else None, override=override)
    totals = p.totals
    ctx.log("Plan: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    ctx.count("matched", totals.get(INCLUDE, 0))
    ctx.count("present", totals.get(PRESENT, 0))
    ctx.count("excluded", totals.get(EXCLUDE, 0))
    for r in p.by_decision(SKIPPED):
        ctx.count("skipped")
        ctx.item(r.base, "skipped", r.rule)

    folder = fsutil.ensure_dir(cfg.system_dir(sysdef.key))
    bios_folder = cfg.bios_dir / sysdef.key
    tmp_root = fsutil.ensure_dir(cfg.temp_dir / f"job-{ctx.id}")
    ex = Extractor(archive, tmp_root)
    try:
        for r in p.rows:
            if r.decision not in (INCLUDE, BIOS):
                continue
            ctx.check()
            try:
                if r.decision == BIOS:
                    fsutil.ensure_dir(bios_folder)
                    if r.kind == INNER:
                        inner = ex.member_to_temp(r.entry, ctx.check)
                        try:
                            _stage_inner_bios(ctx, cfg, sysdef, inner, bios_folder, r)
                        finally:
                            shutil.rmtree(inner.parent, ignore_errors=True)
                    else:
                        ex.write_member(r.entry, bios_folder / r.base, ctx.check)
                        ctx.count("bios_staged")
                        ctx.item(r.base, "bios", f"staged to _bios/{sysdef.key}/")
                        ctx.log(f"BIOS {r.base} -> _bios/{sysdef.key}/")
                elif r.kind == DISC:
                    _add_disc_set(ctx, cfg, sysdef, ex, r, folder, tmp_root, convert)
                elif r.kind in (ROM, SET):
                    ex.write_member(r.entry, folder / r.base, ctx.check)
                    existing_stems_cache(ctx, folder).add(Path(r.base).stem.casefold())
                    ctx.count("added")
                    ctx.item(r.base, "added", r.rule)
                    ctx.log(f"ADD  {r.base}")
                elif r.kind == INNER:
                    stem = Path(r.base).stem.casefold()
                    if stem in existing_stems_cache(ctx, folder):
                        ctx.count("present")
                        continue
                    inner = ex.member_to_temp(r.entry, ctx.check)
                    try:
                        if convert:
                            game = chd.Game(Path(r.base).stem, "archive", inner)
                            chd.convert_game(ctx, cfg, sysdef, game, backup=False)  # the source zip is the backup
                        else:
                            _unpack_inner(ctx, cfg, sysdef, inner, folder, r)
                    finally:
                        shutil.rmtree(inner.parent, ignore_errors=True)
            except FileExistsError as e:
                ctx.count("present")
                ctx.item(r.base, "present", f"not overwritten: {Path(str(e)).name}")
            except Exception as e:  # noqa: BLE001 - one bad entry must not stop the run
                if type(e).__name__ == "Cancelled":
                    raise
                ctx.count("failed")
                ctx.item(r.base, "failed", f"{type(e).__name__}: {e}")
                ctx.log(f"FAIL {r.base}: {e}")
    finally:
        ex.close()
        shutil.rmtree(tmp_root, ignore_errors=True)
        ctx.log("Applying ownership/permissions")
        fsutil.fix_perms(folder, recursive=True)
        if bios_folder.exists():
            fsutil.fix_perms(bios_folder, recursive=True)

    for hook in sysdef.hooks:
        fn = HOOKS.get(hook)
        if fn:
            ctx.log(f"Running {hook} hook")
            fn(ctx, cfg, folder)
            fsutil.fix_perms(folder, recursive=True)
    return {}


# ---- import into _source ---------------------------------------------------------

def source_name(system: str, archive: Path) -> str:
    return f"{system}-roms{Path(archive).suffix.lower()}"


def import_targets(cfg: Config, system: str, archive: Path) -> tuple[Path, Path]:
    """(main source name, where an additional archive for the same system goes)."""
    archive = Path(archive)
    return cfg.source_dir / source_name(system, archive), cfg.source_dir / system / archive.name


def _same_file(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and fsutil.md5_file(a) == fsutil.md5_file(b)


def import_archive(ctx, cfg: Config, system: str, archive: Path, mode: str) -> Path:
    """Store an archive in _source. Returns the path to extract from.

    The first archive for a system becomes _source/<system>-roms.<ext>. When that
    already exists with different content, the new archive is stored as
    _source/<system>/<its own name> instead; existing source archives are never
    modified or overwritten. An identical copy already in either place is reused.

    mode: "none" (extract in place), "copy", or "move" (copy-verify-remove).
    """
    archive = Path(archive)
    if mode == "none":
        return archive
    if fsutil.is_within(archive, cfg.source_dir):
        if mode == "move":
            raise RuntimeError("refusing to move a file that is already inside _source; use copy")
        ctx.log(f"{archive.name} is already inside _source; using it where it is")
        return archive
    main, extra = import_targets(cfg, system, archive)
    dest = main
    if main.exists():
        if _same_file(main, archive):
            ctx.log(f"_source/{main.name} already exists with identical content; using it")
            return main
        dest = extra
        ctx.log(f"_source/{main.name} already exists (different content); storing this archive as "
                f"_source/{system}/{archive.name}")
        if extra.exists():
            if _same_file(extra, archive):
                ctx.log(f"_source/{system}/{archive.name} already exists with identical content; using it")
                return extra
            raise RuntimeError(
                f"_source/{system}/{archive.name} already exists with different content; not overwriting. "
                "Rename the new archive, or extract it without importing."
            )
    fsutil.ensure_dir(dest.parent)
    rel = dest.relative_to(cfg.source_dir).as_posix()
    ctx.log(f"Importing {archive} -> _source/{rel} ({mode})")
    if mode == "move":
        fsutil.safe_move(archive, dest, ctx.check)
    else:
        fsutil.copy_verify(archive, dest, ctx.check)
    ctx.log("Import verified (size + MD5)")
    ctx.count("imported")
    return dest


def keep_in_source(state, ctx) -> dict:
    """Copy an archive into _source under its own name (copy, verify; never overwrite).

    With ``system`` it goes to _source/<system>/, next to that system's per-game sources.
    """
    cfg = state.cfg
    src = Path(ctx.params["archive"])
    system = ctx.params.get("system")
    if system:
        state.system(system)  # must be a defined system folder name
    dest = (cfg.source_dir / system / src.name) if system else (cfg.source_dir / src.name)
    rel = dest.relative_to(cfg.source_dir).as_posix()
    if dest.exists():
        if dest.stat().st_size == src.stat().st_size and fsutil.md5_file(dest) == fsutil.md5_file(src):
            ctx.log(f"_source/{rel} already exists with identical content")
            return {}
        raise RuntimeError(f"_source/{rel} already exists with different content; not overwriting")
    ready = fsutil.wait_stable(src, cfg.stability.wait_seconds)
    if not ready.ok:
        raise RuntimeError(f"{src.name} is not ready: {ready.reason}")
    fsutil.ensure_dir(dest.parent)
    fsutil.copy_verify(src, dest, ctx.check)
    ctx.count("kept")
    ctx.log(f"Copied {src} -> _source/{rel} (MD5 verified)")
    return {}


def uploads_dir(cfg: Config) -> Path:
    return cfg.manager_dir / "uploads"


def queue_upload_cleanup(state, path: Path, job_ids: list[int]) -> int | None:
    """After an import from _manager/uploads, tidy the upload once those jobs have succeeded."""
    path = Path(path)
    if not job_ids or not fsutil.is_within(path, uploads_dir(state.cfg)) or \
            path.resolve() == uploads_dir(state.cfg).resolve():
        return None
    return state.queue.submit("upload_cleanup", f"Clean up upload {path.name}", {"path": str(path), "after": job_ids})


def cleanup_upload(state, ctx) -> dict:
    """Move an upload to the trash once every job that used it finished successfully.

    The upload may be the only copy of an archive (no "keep a copy", or a folder upload),
    so it goes to _trash/ (restorable) rather than being deleted, and it is left in place
    entirely if any of those jobs failed or was cancelled.
    """
    import time

    from .jobs import TERMINAL

    cfg = state.cfg
    path = Path(ctx.params["path"])
    after = [int(j) for j in ctx.params.get("after", [])]
    if not fsutil.is_within(path, uploads_dir(cfg)) or path.resolve() == uploads_dir(cfg).resolve():
        raise RuntimeError(f"{path} is not inside _manager/uploads; refusing to touch it")
    while True:
        jobs = [state.queue.get(j) for j in after]
        if all(j and j["status"] in TERMINAL for j in jobs):
            break
        ctx.check()
        time.sleep(2)
    bad = [j for j in jobs if j["status"] != "done"]
    if bad:
        detail = ", ".join(f"#{j['id']} {j['status']}" for j in bad)
        ctx.log(f"Not cleaning up {path.name}: job(s) {detail}. The upload stays so you can retry.")
        ctx.count("kept")
        return {"left_in_place": detail}
    if not path.exists():
        ctx.log(f"{path.name} is already gone (e.g. moved into _source); nothing to do")
        return {}
    batch = fsutil.new_trash_batch(cfg.trash_dir)
    dest = fsutil.move_to_trash(path, cfg.roms, batch)
    ctx.count("trashed")
    ctx.log(f"All {len(after)} job(s) succeeded; moved the upload to {dest} (restore it from the Trash page)")
    state.library.invalidate()
    return {}


def run_job(state, ctx) -> dict:
    prm = ctx.params
    sysdef = state.system(prm["system"])
    archive = Path(prm["archive"])
    cfg = state.cfg
    if prm.get("import_mode", "none") != "none":
        ready = fsutil.wait_stable(archive, cfg.stability.wait_seconds)
        if not ready.ok:
            raise RuntimeError(f"{archive.name} is not ready: {ready.reason}")
        archive = import_archive(ctx, cfg, sysdef.key, archive, prm["import_mode"])
    result = execute(ctx, cfg, sysdef, archive, select=prm.get("select"), convert=prm.get("convert"),
                     override=prm.get("override", True))
    state.library.invalidate()
    return result


def plan_dict(p: Plan) -> dict:
    return {"archive": p.archive, "system": p.system, "rows": [asdict(r) for r in p.rows]}
