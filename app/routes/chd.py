"""Disc → CHD: choose input + system, dry run, run."""
from __future__ import annotations

import shutil
import shlex
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request

from .. import fsutil
from ..chd import discover
from . import redirect, render, st

router = APIRouter()


def system_mismatch(s, p: Path, system: str) -> str | None:
    """Input inside another system's game folder: almost certainly the wrong system."""
    for key in s.systems:
        if key != system and fsutil.is_within(p, s.cfg.system_dir(key)):
            return (f"{p} is inside the {key} folder, but {system} is selected. "
                    f"Choose {key} so the CHDs and backups go to the right place.")
    return None


def _disc_systems(s):
    return {k: v for k, v in s.systems.items() if v.disc}


@router.get("/chd")
def chd_page(request: Request, system: str = "", path: str = ""):
    s = st(request)
    chdman = shlex.split(s.cfg.tools.chdman)[0] if s.cfg.tools.chdman else ""
    return render(request, "chd.html", systems=_disc_systems(s), system=system, path=path,
                  chdman_found=bool(chdman and shutil.which(chdman)))


@router.post("/chd/plan")
def chd_plan(request: Request, path: str = Form(...), system: str = Form(...)):
    s = st(request)
    p = s.check_browsable(path.strip())
    if not p.exists():
        raise HTTPException(400, f"{p} does not exist")
    sysdef = s.system(system)
    if not sysdef.disc:
        raise HTTPException(400, f"{system} is not a disc system")
    problem = system_mismatch(s, p, system)
    if problem:
        return render(request, "partials/flash.html", kind="error", message=problem)
    games = discover(s.cfg, sysdef, p)
    totals = Counter(g.status for g in games)
    return render(request, "partials/chd_plan.html", games=games, totals=totals, path=str(p), sysdef=sysdef)


@router.post("/chd/run")
def chd_run(request: Request, path: str = Form(...), system: str = Form(...), only: list[str] = Form(default=[])):
    s = st(request)
    p = s.check_browsable(path.strip())
    sysdef = s.system(system)
    if not only:
        raise HTTPException(400, "no games selected")
    problem = system_mismatch(s, p, system)
    if problem:
        raise HTTPException(400, problem)
    job_id = s.queue.submit("chd", f"CHD {p.name} → {system}", {"input": str(p), "system": sysdef.key, "only": only},
                            system=sysdef.key)
    return redirect(request, f"/jobs/{job_id}")
