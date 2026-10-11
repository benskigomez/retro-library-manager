"""Arcade tools: MAME listxml import, verify, classify, hide, lookup, bundle install."""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import archives, extract, fsutil, gamelist
from ..arcade.jobs import hide_names, mame
from ..arcade.mamedb import Have, verify_set
from . import redirect, render, st

router = APIRouter()


def _arcade_systems(s):
    return [k for k, v in s.systems.items() if v.arcade]


def _system(s, system: str) -> str:
    if system not in _arcade_systems(s):
        raise HTTPException(400, f"{system} is not an arcade system")
    return system


@router.get("/arcade")
def arcade_page(request: Request):
    s = st(request)
    m = mame(s)
    last_verify = next((j for j in s.queue.recent(200) if j["kind"] == "mame_verify" and j["status"] == "done"), None)
    return render(request, "arcade.html", meta=m.meta() if m.loaded else {}, loaded=m.loaded,
                  systems=_arcade_systems(s), last_verify=last_verify)


@router.put("/arcade/upload")
async def upload_xml(request: Request, name: str):
    s = st(request)
    base = Path(name.replace("\\", "/")).name
    if not base.lower().endswith(".xml") or base.startswith("."):
        raise HTTPException(400, "upload the .xml produced by mame -listxml")
    updir = fsutil.ensure_dir(s.cfg.manager_dir / "uploads")
    dest = updir / base
    if dest.exists():
        raise HTTPException(409, f"uploads/{base} already exists; pick it with the file browser")
    part = updir / f".{base}.part"
    try:
        with open(part, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        os.rename(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    fsutil.fix_perms(dest)
    return JSONResponse({"path": str(dest)})


@router.post("/arcade/import")
def import_xml(request: Request, path: str = Form(...)):
    s = st(request)
    p = s.check_browsable(path.strip())
    if not p.is_file():
        raise HTTPException(400, f"{p} is not a file")
    job_id = s.queue.submit("mame_import", f"Import MAME listxml {p.name}", {"xml": str(p)})
    return redirect(request, f"/jobs/{job_id}")


@router.post("/arcade/verify")
def verify(request: Request, system: str = Form("mame")):
    s = st(request)
    system = _system(s, system)
    job_id = s.queue.submit("mame_verify", f"Verify {system} sets", {"system": system}, system=system)
    return redirect(request, f"/jobs/{job_id}")


@router.get("/arcade/results/{job_id}")
def results(request: Request, job_id: int, status: str = "", cls: str = ""):
    s = st(request)
    job = s.queue.get(job_id)
    if not job or job["kind"] != "mame_verify":
        raise HTTPException(404, "not a verify job")
    items = s.queue.items(job_id)
    counts = Counter(i["status"] for i in items)
    classes = Counter(i["data"].get("cls", "unknown") for i in items)
    shown = [i for i in items if (not status or i["status"] == status) and (not cls or i["data"].get("cls") == cls)]
    return render(request, "arcade_results.html", job=job, items=shown, counts=counts, classes=classes,
                  status=status, cls=cls)


@router.post("/arcade/hide/preview")
def hide_preview(request: Request, system: str = Form("mame")):
    s = st(request)
    system = _system(s, system)
    m = mame(s)
    if not m.loaded:
        return render(request, "partials/flash.html", kind="error", message="Import a MAME -listxml first.")
    folder = s.cfg.system_dir(system)
    names = hide_names(m, folder)
    changes = gamelist.plan_hide(folder / "gamelist.xml", names)
    classes = {n: m.classify(Path(n).stem) for n in names}
    return render(request, "partials/hide_preview.html", changes=changes, classes=classes, system=system,
                  exists=(folder / "gamelist.xml").exists())


@router.post("/arcade/hide")
def hide(request: Request, system: str = Form("mame"), confirm_closed: str = Form("")):
    s = st(request)
    system = _system(s, system)
    if not confirm_closed:
        raise HTTPException(400, "confirm that RetroBat is fully closed")
    job_id = s.queue.submit("mame_hide", f"Hide BIOS/device/mechanical sets in {system}/gamelist.xml",
                            {"system": system, "confirm_closed": True}, system=system)
    return redirect(request, f"/jobs/{job_id}")


@router.get("/arcade/lookup")
def lookup(request: Request, q: str = "", system: str = "mame"):
    s = st(request)
    m = mame(s)
    rows = []
    if q.strip() and m.loaded:
        have = Have(s.cfg.system_dir(_system(s, system)))
        for r in m.search(q.strip()):
            name = r["name"]
            res = verify_set(m, have, name) if have.has(name) else None
            devices = [d for d in m.all_devices(name) if m.roms(d)]
            needed = [name] + m.romof_chain(name) + devices
            rows.append({
                "m": r, "cls": m.classify(name), "parent": r["cloneof"], "bios": m.bios_of(name),
                "devices": devices, "have": {n: have.has(n) for n in needed},
                "complete": res.status == "good" if res else False, "verified": res is not None,
                "missing": res.missing if res else [],
            })
    return render(request, "partials/lookup.html", rows=rows, q=q, loaded=m.loaded)


@router.post("/arcade/bundle/preview")
def bundle_preview(request: Request, path: str = Form(...), system: str = Form("mame")):
    s = st(request)
    system = _system(s, system)
    p = s.check_browsable(path.strip())
    if not p.is_file() or p.suffix.lower() not in archives.ARCHIVE_EXTS:
        raise HTTPException(400, "pick a .zip or .7z bundle")
    plan = extract.plan(s.cfg, s.system(system), p, content=s.content_index(system))
    return render(request, "partials/bundle_preview.html", plan=plan, path=str(p), system=system)


@router.post("/arcade/bundle")
def bundle(request: Request, path: str = Form(...), system: str = Form("mame"), hide: str = Form(""),
           confirm_closed: str = Form("")):
    s = st(request)
    system = _system(s, system)
    p = s.check_browsable(path.strip())
    if hide and not confirm_closed:
        raise HTTPException(400, "confirm that RetroBat is fully closed to re-run hiding")
    job_id = s.queue.submit("mame_bundle", f"Install arcade bundle {p.name} → {system}",
                            {"archive": str(p), "system": system, "hide": bool(hide), "confirm_closed": bool(confirm_closed)},
                            system=system)
    return redirect(request, f"/jobs/{job_id}")
