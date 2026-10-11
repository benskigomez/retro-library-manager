"""Source browser: list _source archives, download entries, copy/convert selected entries into the library."""
from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .. import archives, fsutil
from ..extract import plan as make_plan
from ..systems import detect_system
from . import redirect, render, st
from .download import _disposition

router = APIRouter()
MAX_ROWS = 500


def _source_file(s, path: str) -> Path:
    p = Path(path)
    if not fsutil.is_within(p, s.cfg.source_dir) or not p.is_file():
        raise HTTPException(404, "not a file in _source")
    return p


def _system_for(s, p: Path) -> str | None:
    """<system>-roms.zip at the top, or a per-game backup in _source/<system>/."""
    m = re.match(r"^(.+?)-roms\.(zip|7z)$", p.name, re.I)
    if m and m.group(1) in s.systems:
        return m.group(1)
    if p.parent.parent.resolve() == s.cfg.source_dir.resolve() and p.parent.name in s.systems:
        return p.parent.name
    return None


@router.get("/source")
def source_home(request: Request):
    s = st(request)
    top, backups = [], []
    if s.cfg.source_dir.is_dir():
        for e in sorted(os.scandir(s.cfg.source_dir), key=lambda e: e.name.casefold()):
            if e.is_file() and Path(e.name).suffix.lower() in archives.ARCHIVE_EXTS:
                p = Path(e.path)
                top.append({"path": e.path, "name": e.name, "size": e.stat().st_size, "system": _system_for(s, p)})
            elif e.is_dir() and not e.name.startswith((".", "_")):
                files = [f for f in os.scandir(e.path) if f.is_file()]
                backups.append({"name": e.name, "count": len(files), "size": sum(f.stat().st_size for f in files),
                                "known": e.name in s.systems})
    return render(request, "source.html", top=top, backups=backups)


@router.get("/source/backups/{system}")
def backup_folder(request: Request, system: str):
    s = st(request)
    folder = s.cfg.source_dir / system
    if "/" in system or "\\" in system or not folder.is_dir():
        raise HTTPException(404, "no such backup folder")
    lib = {e.name.casefold() for e in os.scandir(s.cfg.system_dir(system))} if s.cfg.system_dir(system).is_dir() else set()
    rows = []
    for e in sorted(os.scandir(folder), key=lambda e: e.name.casefold()):
        if e.is_file():
            stem = Path(e.name).stem
            rows.append({"path": e.path, "name": e.name, "size": e.stat().st_size,
                         "in_library": any(n.startswith(stem.casefold() + ".") for n in lib)})
    sysdef = s.systems.get(system)
    return render(request, "source_backups.html", system=system, sysdef=sysdef, rows=rows)


@router.get("/source/view")
def view(request: Request, path: str, system: str = "", q: str = "", show: str = ""):
    s = st(request)
    p = _source_file(s, path)
    system = system or _system_for(s, p) or ""
    entries = [e for e in archives.list_entries(p) if not e.is_dir]
    if not system:
        ranking = detect_system([e.name for e in entries], s.systems)
        system = ranking[0][0] if ranking else ""
    sysdef = s.systems.get(system)
    if sysdef:
        rows = make_plan(s.cfg, sysdef, p, entries, content=s.content_index(system)).rows
    else:
        rows = []
    totals = Counter(r.decision for r in rows)
    ql = q.casefold().strip()
    shown = [r for r in rows if (not ql or ql in r.entry.casefold()) and (not show or r.decision == show)]
    return render(request, "source_view.html", path=str(p), name=p.name, system=system, sysdef=sysdef,
                  systems=s.systems, rows=shown[:MAX_ROWS], total=len(shown), totals=totals, q=q, show=show,
                  in_backups=p.parent.resolve() != s.cfg.source_dir.resolve(),
                  sizes={e.name: e.size for e in entries})


@router.get("/source/download")
def download(request: Request, path: str, member: str = ""):
    s = st(request)
    p = _source_file(s, path)
    if not member:
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")
    names = {e.name for e in archives.list_entries(p) if not e.is_dir}
    if member not in names:
        raise HTTPException(404, "no such entry in the archive")
    base = member.rstrip("/").rsplit("/", 1)[-1]
    return StreamingResponse(archives.stream_member(p, member), media_type="application/octet-stream",
                             headers={"Content-Disposition": _disposition(base)})


@router.post("/source/preview")
def preview(request: Request, path: str = Form(...), system: str = Form(...), mode: str = Form("copy"),
            names: list[str] = Form(default=[])):
    s = st(request)
    p = _source_file(s, path)
    sysdef = s.system(system)
    if mode == "chd_file":
        from ..chd import discover

        games = discover(s.cfg, sysdef, p)
        return render(request, "partials/source_preview.html", mode=mode, games=games, path=str(p), system=system,
                      names=[], rows=[], totals=Counter(g.status for g in games))
    if not names:
        return render(request, "partials/flash.html", kind="warn", message="Select at least one entry.")
    rows = make_plan(s.cfg, sysdef, p, select=set(names), content=s.content_index(system)).rows
    return render(request, "partials/source_preview.html", mode=mode, rows=rows, path=str(p), system=system,
                  names=names, games=[], totals=Counter(r.decision for r in rows), sysdef=sysdef)


@router.post("/source/run")
def run(request: Request, path: str = Form(...), system: str = Form(...), mode: str = Form("copy"),
        names: list[str] = Form(default=[])):
    s = st(request)
    p = _source_file(s, path)
    sysdef = s.system(system)
    if mode == "chd_file":
        if not sysdef.disc:
            raise HTTPException(400, f"{system} is not a disc system")
        job_id = s.queue.submit("chd", f"CHD {p.name} → {system}", {"input": str(p), "system": system, "only": []},
                                system=system)
        return redirect(request, f"/jobs/{job_id}")
    if not names:
        raise HTTPException(400, "nothing selected")
    convert = mode == "chd"
    if convert and not sysdef.disc:
        raise HTTPException(400, f"{system} is not a disc system")
    verb = "Convert" if convert else "Copy"
    job_id = s.queue.submit("extract", f"{verb} {len(names)} entr{'y' if len(names) == 1 else 'ies'} from {p.name} → {system}",
                            {"archive": str(p), "system": system, "import_mode": "none", "select": names,
                             "convert": convert}, system=system)
    return redirect(request, f"/jobs/{job_id}")
