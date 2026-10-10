"""Registers job handlers with the queue."""
from __future__ import annotations

from .state import State


def register_handlers(state: State) -> None:
    from . import bios, chd, extract

    q = state.queue
    q.register("extract")(lambda ctx: extract.run_job(state, ctx))
    q.register("chd")(lambda ctx: chd.run_job(state, ctx))
    q.register("bios_install")(lambda ctx: bios.run_install_mounted(state, ctx))
    q.register("bios_export")(lambda ctx: bios.run_export(state, ctx))
    q.register("bios_stage")(lambda ctx: bios.run_stage_from_archive(state, ctx))
    q.register("lynx_regen")(lambda ctx: extract_lynx_regen(state, ctx))


def extract_lynx_regen(state: State, ctx) -> dict:
    """Rebuild selected .lnx files (old ones go to the trash) with the current rotation settings."""
    from pathlib import Path

    from . import fsutil
    from .hooks import lynx

    cfg = state.cfg
    folder = cfg.system_dir("lynx")
    batch = None
    for stem in ctx.params["stems"]:
        ctx.check()
        lyx, lnx = folder / f"{stem}.lyx", folder / f"{stem}.lnx"
        if not lyx.is_file():
            ctx.item(stem, "skipped", "no .lyx source")
            ctx.count("skipped")
            continue
        if lnx.exists():
            batch = batch or fsutil.new_trash_batch(cfg.trash_dir)
            fsutil.move_to_trash(lnx, cfg.roms, batch)
            ctx.log(f"old {lnx.name} moved to trash/{batch.name}")
        rot = lynx.rotation_for(stem, cfg.lynx.rotation)
        note = lynx.write_lnx(lyx, Path(lnx), cfg.lynx.manufacturer, rot)
        ctx.item(lnx.name, "added", note)
        ctx.count("regenerated")
        ctx.log(f"{lnx.name}: {note}")
    state.library.invalidate()
    return {}
