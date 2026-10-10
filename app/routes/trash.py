"""Trash: list batches, restore (no overwrite), purge (with preview)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from .. import fsutil
from . import redirect, render, st

router = APIRouter()


def _batch(s, name: str) -> Path:
    p = s.cfg.trash_dir / name
    if "/" in name or "\\" in name or name.startswith(".") or not p.is_dir():
        raise HTTPException(404, "no such trash batch")
    return p


def _files(batch: Path) -> list[dict]:
    out = []
    for root, _d, files in os.walk(batch):
        for f in files:
            p = Path(root) / f
            out.append({"rel": str(p.relative_to(batch)).replace("\\", "/"), "size": p.stat().st_size})
    return sorted(out, key=lambda r: r["rel"])


@router.get("/trash")
def trash_home(request: Request):
    s = st(request)
    batches = []
    if s.cfg.trash_dir.is_dir():
        for e in sorted(os.scandir(s.cfg.trash_dir), key=lambda e: e.name, reverse=True):
            if e.is_dir():
                size, count = fsutil.tree_size(Path(e.path))
                batches.append({"name": e.name, "size": size, "count": count})
    return render(request, "trash.html", batches=batches, msg=request.query_params.get("msg"))


@router.get("/trash/{name}")
def trash_batch(request: Request, name: str, action: str = ""):
    s = st(request)
    batch = _batch(s, name)
    files = _files(batch)
    for f in files:
        f["conflict"] = (s.cfg.roms / f["rel"]).exists()
    return render(request, "trash_batch.html", name=name, files=files, action=action)


@router.post("/trash/{name}/restore")
def trash_restore(request: Request, name: str):
    s = st(request)
    restored, skipped = fsutil.restore_from_trash(_batch(s, name), s.cfg.roms)
    s.library.invalidate()
    msg = f"Restored {len(restored)} file(s)" + (f"; {len(skipped)} skipped because the target exists" if skipped else "")
    return redirect(request, "/trash?msg=" + quote(msg))


@router.post("/trash/{name}/purge")
def trash_purge(request: Request, name: str):
    s = st(request)
    batch = _batch(s, name)
    size, count = fsutil.tree_size(batch)
    shutil.rmtree(batch)
    return redirect(request, "/trash?msg=" + quote(f"Permanently deleted {count} file(s) ({size} bytes) from {name}"))
