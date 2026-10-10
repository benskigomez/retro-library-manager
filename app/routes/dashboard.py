"""Library dashboard and per-system pages."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request

from .. import download, fsutil
from ..library import _is_meta, scan_system
from . import redirect, render, st

router = APIRouter()


@router.get("/")
def dashboard(request: Request, rescan: int = 0):
    s = st(request)
    scan = s.library.get(s.cfg, s.systems, force=bool(rescan))
    last_jobs = s.queue.last_by_system()
    return render(request, "dashboard.html", scan=scan, last_jobs=last_jobs)


@router.get("/system/{key}")
def system_page(request: Request, key: str, show: str = "all", q: str = ""):
    s = st(request)
    if key not in s.systems:
        raise HTTPException(404, f"unknown system {key}")
    sysdef = s.systems[key]
    status = scan_system(s.cfg, sysdef)
    folder = s.cfg.system_dir(key)
    files = []
    ql = q.casefold().strip()
    if folder.is_dir():
        accepted = sysdef.rom_extensions
        for e in sorted(os.scandir(folder), key=lambda e: e.name.casefold()):
            if not e.is_file(follow_symlinks=False) or e.name.startswith("."):
                continue
            ext = Path(e.name).suffix.lower()
            if _is_meta(e.name):
                kind = "meta"
            else:
                kind = "game" if ext in accepted else ("archive" if ext in (".zip", ".7z") else "other")
            if show == "flagged" and kind in ("game", "meta"):
                continue
            multi = ext in (".cue", ".gdi", ".m3u") and len(download.game_files(Path(e.path))) > 1
            if ql and ql not in e.name.casefold():
                continue
            files.append({"name": e.name, "size": e.stat().st_size, "kind": kind,
                          "dl_name": (Path(e.name).stem + ".zip") if multi else e.name, "is_chd": ext == ".chd"})
    jobs = s.queue.recent(20, system=key)
    filters = s.cfg.filters.systems.get(key)
    return render(request, "system.html", sysdef=sysdef, status=status, files=files, jobs=jobs,
                  show=show, filters=filters, q=q)


@router.post("/system/{key}/trash/preview")
def trash_preview(request: Request, key: str, names: list[str] = Form(default=[])):
    s = st(request)
    folder = s.cfg.system_dir(key)
    rows = []
    for n in names:
        p = folder / n
        if not fsutil.is_within(p, folder) or not p.is_file():
            rows.append({"name": n, "ok": False, "size": 0})
        else:
            rows.append({"name": n, "ok": True, "size": p.stat().st_size})
    return render(request, "partials/trash_preview.html", key=key, rows=rows)


@router.post("/system/{key}/trash")
def trash_files(request: Request, key: str, names: list[str] = Form(default=[])):
    s = st(request)
    folder = s.cfg.system_dir(key)
    targets = [folder / n for n in names if fsutil.is_within(folder / n, folder) and (folder / n).is_file()]
    if targets:
        batch = fsutil.new_trash_batch(s.cfg.trash_dir)
        for p in targets:
            fsutil.move_to_trash(p, s.cfg.roms, batch)
        s.library.invalidate()
    return redirect(request, f"/system/{key}?trashed={len(targets)}")
