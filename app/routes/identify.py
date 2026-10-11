"""Identify: import an archive or folder whose system(s) you don't know."""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import archives, fsutil
from ..extract import plan as make_plan
from ..identify import identify, summarize
from . import redirect, render, st

router = APIRouter()


def _source(s, path: str) -> Path:
    p = s.check_browsable(path.strip())
    if p.is_dir():
        return p
    if not p.is_file() or p.suffix.lower() not in archives.ARCHIVE_EXTS:
        raise HTTPException(400, "pick a .zip/.7z archive or a folder")
    return p


@router.put("/identify/upload-file")
async def upload_file(request: Request, batch: str, rel: str):
    """One file of a folder uploaded from the browser, kept under its relative path."""
    s = st(request)
    if not re.fullmatch(r"[0-9A-Za-z-]{1,40}", batch):
        raise HTTPException(400, "bad batch id")
    root = fsutil.ensure_dir(s.cfg.manager_dir / "uploads" / f"folder-{batch}")
    parts = [x for x in rel.replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or ".." in parts or any(x.startswith(".") for x in parts):
        raise HTTPException(400, f"bad relative path: {rel}")
    dest = root.joinpath(*parts)
    if not fsutil.is_within(dest, root):
        raise HTTPException(400, f"bad relative path: {rel}")
    if dest.exists():
        raise HTTPException(409, f"{rel} was already uploaded in this batch")
    fsutil.ensure_dir(dest.parent)
    part = dest.with_name(f".{dest.name}.part")
    try:
        with open(part, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        os.rename(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    fsutil.fix_perms(dest)
    return JSONResponse({"path": str(root)})


@router.get("/identify")
def identify_page(request: Request, path: str = ""):
    return render(request, "identify.html", path=path)


@router.post("/identify/scan")
def scan(request: Request, path: str = Form(...)):
    s = st(request)
    p = _source(s, path)
    ready = fsutil.wait_stable(p, s.cfg.stability.wait_seconds)
    if not ready.ok:
        return render(request, "partials/flash.html", kind="error", message=f"{p.name} is not ready: {ready.reason}")
    idents = identify(p, s.systems, {e.lower() for e in s.cfg.ignore_extensions})
    decided, ambiguous, unknown = summarize(idents)
    assigned = {i.entry: i.system for i in idents if i.system}
    amb_groups = [{"candidates": list(c), "items": items, "members": json.dumps([i.entry for i in items]),
                   "suggestion": next((i.suggestion for i in items if i.suggestion), None),
                   "how": Counter(i.how for i in items).most_common(1)[0][0]}
                  for c, items in ambiguous.items()]
    can_keep = p.is_file() and not fsutil.is_within(p, s.cfg.source_dir)
    single_system = next(iter(decided)) if len(decided) == 1 and not amb_groups else None
    return render(request, "partials/identify_result.html", path=str(p), decided=decided, amb_groups=amb_groups,
                  unknown=unknown, assigned=json.dumps(assigned), systems=s.systems, can_keep=can_keep,
                  single_system=single_system)


def _assignments(form) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for entry, system in json.loads(form.get("assigned") or "{}").items():
        groups.setdefault(system, []).append(entry)
    i = 0
    while f"amb_members_{i}" in form:
        system = form.get(f"amb_{i}") or ""
        if system and system != "skip":
            groups.setdefault(system, []).extend(json.loads(form[f"amb_members_{i}"]))
        i += 1
    return groups


@router.post("/identify/plan")
async def plan_view(request: Request):
    s = st(request)
    form = await request.form()
    p = _source(s, form["path"])
    groups = _assignments(form)
    plans = []
    for system, entries in sorted(groups.items()):
        sysdef = s.system(system)
        pl = make_plan(s.cfg, sysdef, p, select=set(entries), override=False)
        plans.append({"system": system, "sysdef": sysdef, "plan": pl, "totals": pl.totals})
    return render(request, "partials/identify_plan.html", path=str(p), plans=plans,
                  groups=json.dumps(groups), keep=bool(form.get("keep")))


@router.post("/identify/run")
async def run(request: Request):
    s = st(request)
    form = await request.form()
    p = _source(s, form["path"])
    assignment: dict[str, list[str]] = json.loads(form["groups"])
    used = [system for system in assignment if form.get(f"use_{system}")]
    if form.get("keep") and p.is_file():
        # Everything for one system: keep it with that system's per-game sources.
        keep_system = used[0] if len(used) == 1 else None
        where = f"_source/{keep_system}/" if keep_system else "_source/"
        s.queue.submit("source_keep", f"Keep a copy of {p.name} in {where}", {"archive": str(p), "system": keep_system})
    for system, entries in sorted(assignment.items()):
        if not form.get(f"use_{system}"):
            continue
        sysdef = s.system(system)
        mode = form.get(f"mode_{system}")
        convert = (mode == "chd") if (sysdef.disc and mode in ("chd", "original")) else None
        n = len(entries)
        s.queue.submit("extract", f"Identify: {n} {'item' if n == 1 else 'items'} from {p.name} → {system}",
                       {"archive": str(p), "system": system, "import_mode": "none", "select": entries,
                        "override": False, "convert": convert}, system=system)
    return redirect(request, "/jobs")
