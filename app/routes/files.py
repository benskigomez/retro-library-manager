"""Server-side file picker restricted to the ROM share and configured browse roots."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request

from . import render, st

router = APIRouter()


@router.get("/files/browse")
def browse(request: Request, path: str = "", target: str = "path", pick: str = "file", exts: str = ".zip,.7z"):
    s = st(request)
    roots = s.browse_roots()
    p = Path(path) if path else None
    entries, parent = [], None
    if p is None:
        entries = [{"name": str(r), "path": str(r), "dir": True, "size": 0} for r in roots if r.is_dir()]
    else:
        s.check_browsable(p)
        if any(p.resolve() == r.resolve() for r in roots):
            parent = ""
        else:
            parent = str(p.parent)
        allowed = {e.strip().lower() for e in exts.split(",") if e.strip()}
        try:
            for e in sorted(os.scandir(p), key=lambda e: (not e.is_dir(), e.name.casefold())):
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    entries.append({"name": e.name, "path": e.path, "dir": True, "size": 0})
                elif pick == "file" and (not allowed or Path(e.name).suffix.lower() in allowed):
                    entries.append({"name": e.name, "path": e.path, "dir": False, "size": e.stat().st_size})
        except OSError as err:
            entries = [{"name": f"cannot read: {err}", "path": "", "dir": False, "size": 0}]
    return render(request, "partials/picker.html", entries=entries, parent=parent, cur=path,
                  target=target, pick=pick, exts=exts)
