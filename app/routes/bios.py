"""BIOS manager: status, staged files, archive scan, install (mounted / export), catalog import."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request

from .. import archives, bios, fsutil
from . import redirect, render, st

router = APIRouter()


def _load(s):
    catalog = bios.load_catalog(s.settings.bios_catalog_path)
    cache = bios.Md5Cache(s.db)
    staged = bios.scan_staged(s.cfg.bios_dir, catalog, cache)
    root = bios.mounted_root(s.cfg)
    rows = bios.build_status(catalog, staged, root, cache)
    return catalog, staged, root, rows


@router.get("/bios")
def bios_page(request: Request):
    s = st(request)
    catalog, staged, root, rows = _load(s)
    script_path = s.cfg.bios_dir / bios.INSTALL_DIR / bios.SCRIPT_NAME
    script = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    return render(request, "bios.html", rows=rows, staged=staged, root=root, summary=bios.system_summary(rows),
                  script=script, catalog_count=len(catalog))


@router.post("/bios/scan-archive")
def scan_archive(request: Request, path: str = Form(...)):
    s = st(request)
    p = s.check_browsable(path.strip())
    if not p.is_file() or p.suffix.lower() not in archives.ARCHIVE_EXTS:
        raise HTTPException(400, "pick a .zip or .7z")
    ready = fsutil.wait_stable(p, s.cfg.stability.wait_seconds)
    if not ready.ok:
        return render(request, "partials/flash.html", kind="error", message=f"{p.name} is not ready: {ready.reason}")
    catalog = bios.load_catalog(s.settings.bios_catalog_path)
    members = bios.scan_archive(p, catalog)
    return render(request, "partials/bios_archive.html", members=members, path=str(p))


@router.post("/bios/stage")
def stage(request: Request, path: str = Form(...)):
    s = st(request)
    p = s.check_browsable(path.strip())
    job_id = s.queue.submit("bios_stage", f"Stage BIOS from {p.name}", {"archive": str(p)})
    return redirect(request, f"/jobs/{job_id}")


@router.get("/bios/install/preview")
def install_preview(request: Request, mode: str):
    s = st(request)
    _catalog, _staged, root, rows = _load(s)
    if mode == "mounted" and root is None:
        return render(request, "partials/flash.html", kind="error",
                      message=f"Desktop BIOS folder not mounted (paths.desktop_bios = '{s.cfg.paths.desktop_bios}'). Use export mode.")
    if mode == "export":
        bundle = s.cfg.bios_dir / bios.INSTALL_DIR
        for r in rows:
            r.action = ("install" if r.source else "missing-source")
            if r.source and (bundle / r.entry.file).exists():
                r.action = "installed"  # already in the bundle
    return render(request, "partials/bios_install_preview.html", rows=rows, mode=mode, root=root)


@router.post("/bios/install")
def install(request: Request, mode: str = Form(...), files: list[str] = Form(default=[])):
    s = st(request)
    if mode == "mounted":
        if not files:
            raise HTTPException(400, "nothing selected")
        job_id = s.queue.submit("bios_install", f"Install {len(files)} BIOS file(s) to desktop", {"files": files})
    elif mode == "export":
        job_id = s.queue.submit("bios_export", "Build BIOS install bundle", {})
    else:
        raise HTTPException(400, "bad mode")
    return redirect(request, f"/jobs/{job_id}")


@router.post("/bios/catalog/preview")
def catalog_preview(request: Request, text: str = Form("")):
    s = st(request)
    try:
        incoming = bios.parse_definition(text)
    except Exception as e:  # noqa: BLE001
        return render(request, "partials/flash.html", kind="error", message=f"Could not parse: {e}")
    catalog = bios.load_catalog(s.settings.bios_catalog_path)
    new = bios.merge_new(catalog, incoming)
    return render(request, "partials/bios_catalog_preview.html", incoming=incoming, new=new, text=text)


@router.post("/bios/catalog/import")
def catalog_import(request: Request, text: str = Form("")):
    s = st(request)
    catalog = bios.load_catalog(s.settings.bios_catalog_path)
    new = bios.merge_new(catalog, bios.parse_definition(text))
    content = bios.append_to_catalog_file(s.settings.bios_catalog_path, new)
    s.settings.save_text(s.settings.bios_catalog_path, content, bios.parse_catalog)
    return redirect(request, "/bios")
