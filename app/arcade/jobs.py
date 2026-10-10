"""Arcade job handlers: listxml import, verify, hide non-games, bundle install."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .. import extract, fsutil, gamelist
from .mamedb import HIDE_CLASSES, Have, Mame, import_listxml, open_db, verify_folder


def mame_db(state):
    if getattr(state, "_mame_db", None) is None:
        state._mame_db = open_db(state.cfg.manager_dir / "mame.db")
    return state._mame_db


def mame(state) -> Mame:
    """Cached reference data; replaced after every import."""
    if getattr(state, "_mame", None) is None:
        state._mame = Mame(mame_db(state))
    return state._mame


def run_import(state, ctx) -> dict:
    path = Path(ctx.params["xml"])
    ctx.log(f"Importing {path} ({path.stat().st_size:,} bytes)")

    def progress(n):
        ctx.log(f"  {n:,} machines…")
        ctx.check()

    result = import_listxml(mame_db(state), path, progress)
    state._mame = None
    ctx.log(f"Imported MAME {result['build']}: {result['machines']:,} machines, {result['roms']:,} ROMs")
    return result


def hide_names(m: Mame, folder: Path) -> dict[str, str]:
    """zip file name -> description, for every BIOS / device / mechanical set present."""
    have = Have(folder)
    out = {}
    for key, path in sorted(have.files.items()):
        cls = m.classify(path.stem)
        if cls in HIDE_CLASSES:
            mm = m.machine(path.stem)
            out[path.name] = (mm or {}).get("description") or path.stem
    return out


def run_verify(state, ctx) -> dict:
    m = mame(state)
    if not m.loaded:
        raise RuntimeError("no MAME reference data; import a -listxml file first")
    system = ctx.params.get("system", "mame")
    folder = state.cfg.system_dir(system)
    ctx.log(f"Verifying sets in {folder} against MAME {m.meta().get('build', '?')}")
    results = verify_folder(m, folder, progress=lambda i: (ctx.log(f"  {i} sets…"), ctx.check()))
    classes = Counter()
    for r in results:
        classes[r.cls] += 1
        ctx.count(r.status)
        if r.status == "missing":
            supply = sorted({x["supplier"] + ".zip" for x in r.missing})
            detail = f"{r.cls}: {r.description} — {len(r.missing)} missing ROM(s), supplied by {', '.join(supply)}"
        elif r.status == "unknown":
            detail = "not in MAME reference data"
        else:
            detail = f"{r.cls}: {r.description}"
        ctx.item(r.name, r.status, detail, **{k: v for k, v in asdict(r).items() if k in ("cls", "missing", "notes", "needs")})
    return {f"class_{k}": v for k, v in classes.items()}


def run_hide(state, ctx) -> dict:
    if not ctx.params.get("confirm_closed"):
        raise RuntimeError("RetroBat must be fully closed before editing gamelist.xml (confirm on the form)")
    m = mame(state)
    if not m.loaded:
        raise RuntimeError("no MAME reference data; import a -listxml file first")
    system = ctx.params.get("system", "mame")
    folder = state.cfg.system_dir(system)
    names = hide_names(m, folder)
    gl = folder / "gamelist.xml"
    bak, changes = gamelist.apply_hide(gl, names)
    ctx.log(f"Backup: {bak}" if bak else "No existing gamelist.xml; created a new one")
    for ch in changes:
        ctx.count(ch.action)
        if ch.action != "already":
            ctx.item(ch.name, "added" if ch.action == "add" else "present", f"hidden ({ch.action})")
    return {"backup": str(bak) if bak else "none"}


def run_bundle(state, ctx) -> dict:
    """Copy inner zips of a BIOS/device bundle into the arcade folder as-is, then optionally re-hide."""
    system = ctx.params.get("system", "mame")
    sysdef = state.system(system)
    if not sysdef.arcade:
        raise RuntimeError(f"{system} is not an arcade system")
    extract.execute(ctx, state.cfg, sysdef, Path(ctx.params["archive"]))
    if ctx.params.get("hide"):
        ctx.log("Re-running hide for BIOS/device/mechanical sets")
        run_hide(state, ctx)
    fsutil.fix_perms(state.cfg.system_dir(system), recursive=True)
    state.library.invalidate()
    return {}
