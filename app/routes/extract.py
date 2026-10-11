"""Import (upload / pick), inspect, dry-run plan, extract; Lynx header tools."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import archives, fsutil
from ..extract import import_targets, plan as make_plan, queue_upload_cleanup
from ..hooks import lynx
from ..identify import Detection, rank_systems
from . import redirect, render, st

router = APIRouter()
MAX_ROWS = 2000


@router.get("/import")
def import_page(request: Request, system: str = "", path: str = ""):
    s = st(request)
    return render(request, "import.html", system=system, path=path, systems=s.systems)


@router.put("/import/upload")
async def upload(request: Request, name: str):
    """Stream a browser upload straight to _manager/uploads (no temp spooling)."""
    s = st(request)
    base = Path(name.replace("\\", "/")).name
    if not base or base.startswith(".") or Path(base).suffix.lower() not in archives.ARCHIVE_EXTS:
        raise HTTPException(400, "only .zip or .7z uploads are accepted")
    updir = fsutil.ensure_dir(s.cfg.manager_dir / "uploads")
    dest = updir / base
    if dest.exists():
        raise HTTPException(409, f"uploads/{base} already exists; pick it with the file browser instead")
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


def _checked_archive(s, path: str) -> Path:
    p = s.check_browsable(path.strip())
    if not p.is_file():
        raise HTTPException(400, f"{p} is not a file")
    if p.suffix.lower() not in archives.ARCHIVE_EXTS:
        raise HTTPException(400, "pick a .zip or .7z archive")
    return p


@router.post("/import/inspect")
def inspect(request: Request, path: str = Form(...), system: str = Form("")):
    s = st(request)
    p = _checked_archive(s, path)
    ready = fsutil.wait_stable(p, s.cfg.stability.wait_seconds)
    if not ready.ok:
        return render(request, "partials/flash.html", kind="error", message=f"{p.name} is not ready: {ready.reason}")
    info = archives.inspect(p)
    ranking = rank_systems(p, s.systems, {e.lower() for e in s.cfg.ignore_extensions})
    m = re.match(r"^(.+?)-roms\.(zip|7z)$", p.name, re.I)
    if m and m.group(1) in s.systems:
        ranking = [d for d in ranking if d.system != m.group(1)]
        ranking.insert(0, Detection(m.group(1), 0, 0, f"stored as {p.name}"))
    chosen = system or (ranking[0].system if ranking else "")
    in_source = fsutil.is_within(p, s.cfg.source_dir)
    return render(request, "partials/inspect.html", info=info, ranking=ranking[:5], chosen=chosen,
                  path=str(p), in_source=in_source, systems=s.systems)


@router.post("/extract/plan")
def plan_view(request: Request, path: str = Form(...), system: str = Form(...),
              import_mode: str = Form("none"), show: str = Form("")):
    s = st(request)
    p = _checked_archive(s, path)
    sysdef = s.system(system)
    plan = make_plan(s.cfg, sysdef, p, content=s.content_index(system))
    rows = [r for r in plan.rows if not show or r.decision == show]
    main, extra = import_targets(s.cfg, system, p)
    target_source = extra if main.exists() else main
    return render(request, "partials/plan.html", plan=plan, rows=rows[:MAX_ROWS], total_rows=len(rows),
                  show=show, path=str(p), system=system, sysdef=sysdef, import_mode=import_mode,
                  target_source=target_source, main_source=main, main_exists=main.exists(),
                  source_exists=target_source.exists())


@router.get("/extract/plan.csv")
def plan_csv(request: Request, path: str, system: str):
    s = st(request)
    p = _checked_archive(s, path)
    plan = make_plan(s.cfg, s.system(system), p, content=s.content_index(system))
    fname = f"plan-{system}-{p.stem}.csv"
    return Response(plan.csv(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/extract/run")
def run(request: Request, path: str = Form(...), system: str = Form(...), import_mode: str = Form("none"),
        disc_mode: str = Form("")):
    s = st(request)
    p = _checked_archive(s, path)
    sysdef = s.system(system)
    if import_mode not in ("none", "copy", "move"):
        raise HTTPException(400, "bad import mode")
    convert = (disc_mode == "chd") if (sysdef.disc and disc_mode in ("chd", "original")) else None
    title = f"Extract {p.name} → {system}" + (f" ({import_mode} into _source)" if import_mode != "none" else "")
    job_id = s.queue.submit("extract", title, {"archive": str(p), "system": system, "import_mode": import_mode,
                                               "convert": convert}, system=system)
    queue_upload_cleanup(s, p, [job_id])
    return redirect(request, f"/jobs/{job_id}")


# ---- Lynx --------------------------------------------------------------------------

def _lynx_rows(s) -> list[dict]:
    folder = s.cfg.system_dir("lynx")
    if not folder.is_dir():
        return []
    names = {e.name.casefold(): e.name for e in os.scandir(folder) if e.is_file()}
    rows = []
    for low, name in sorted(names.items()):
        if not low.endswith(".lyx"):
            continue
        stem = name[:-4]
        lnx = folder / f"{stem}.lnx"
        current = lynx.read_rotation(lnx) if lnx.exists() else None
        wanted = lynx.rotation_for(stem, s.cfg.lynx.rotation)
        rows.append({"stem": stem, "has_lnx": lnx.exists(), "current": current, "wanted": wanted,
                     "stale": lnx.exists() and current != wanted})
    return rows


@router.get("/extract/lynx")
def lynx_page(request: Request):
    s = st(request)
    return render(request, "lynx.html", rows=_lynx_rows(s), overrides=s.cfg.lynx.rotation)


@router.post("/extract/lynx/rotation")
def lynx_rotation(request: Request, title: str = Form(...), rotation: str = Form(...)):
    s = st(request)
    if rotation not in ("left", "right", "none", "remove"):
        raise HTTPException(400, "bad rotation")
    title = title.strip()

    def mutate(d):
        lx = d.setdefault("lynx", {})
        rot = lx.get("rotation") or {}
        if rotation == "remove":
            rot.pop(title, None)
        else:
            rot[title] = rotation
        lx["rotation"] = rot

    s.settings.update_config(mutate)
    s.reload()
    return redirect(request, "/extract/lynx")


@router.post("/extract/lynx/preview")
def lynx_preview(request: Request, stems: list[str] = Form(default=[])):
    s = st(request)
    rows = [r for r in _lynx_rows(s) if r["stem"] in set(stems)]
    return render(request, "partials/lynx_preview.html", rows=rows)


@router.post("/extract/lynx/regen")
def lynx_regen(request: Request, stems: list[str] = Form(default=[])):
    s = st(request)
    if not stems:
        raise HTTPException(400, "nothing selected")
    job_id = s.queue.submit("lynx_regen", f"Regenerate {len(stems)} Lynx header(s)", {"stems": stems}, system="lynx")
    return redirect(request, f"/jobs/{job_id}")
