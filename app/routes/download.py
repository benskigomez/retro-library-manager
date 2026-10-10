"""Download a game to the operator's device (zipped when multi-file; CHD convertible)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .. import download
from . import redirect, st

router = APIRouter()


def _disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _send(files: list[Path], base: Path, zip_name: str):
    if len(files) == 1:
        return FileResponse(files[0], filename=files[0].name, media_type="application/octet-stream")
    total = sum(f.stat().st_size for f in files)
    return StreamingResponse(download.stream_zip(files, base), media_type="application/zip",
                             headers={"Content-Disposition": _disposition(zip_name),
                                      "X-Uncompressed-Size": str(total)})


@router.get("/download/prepared/{job_id}")
def prepared(request: Request, job_id: int):
    s = st(request)
    job = s.queue.get(job_id)
    if not job or job["kind"] != "download_convert" or job["status"] != "done":
        raise HTTPException(404, "nothing prepared for this job")
    files = download.prepared_files(s.cfg, job_id)
    if not files or not download.dir_age_ok(files[0].parent):
        raise HTTPException(410, "the prepared files have expired; convert again")
    return _send(files, files[0].parent, Path(job["params"]["name"]).stem + ".zip")


@router.get("/download/{system}/{name}")
def get_game(request: Request, system: str, name: str):
    s = st(request)
    s.system(system)
    folder = s.cfg.system_dir(system)
    try:
        path = download.resolve(folder, name)
    except FileNotFoundError:
        raise HTTPException(404, f"{name} not found in {system}") from None
    return _send(download.game_files(path), folder, path.stem + ".zip")


@router.post("/download/{system}/{name}/convert")
def convert(request: Request, system: str, name: str, format: str = Form(...)):
    s = st(request)
    sysdef = s.system(system)
    if format not in ("cue", "iso"):
        raise HTTPException(400, "format must be cue or iso")
    try:
        path = download.resolve(s.cfg.system_dir(system), name)
    except FileNotFoundError:
        raise HTTPException(404, f"{name} not found in {system}") from None
    if path.suffix.lower() != ".chd":
        raise HTTPException(400, "only .chd files are converted")
    job_id = s.queue.submit("download_convert", f"Convert {name} to {format.upper()} for download",
                            {"system": sysdef.key, "name": name, "format": format}, system=sysdef.key)
    return redirect(request, f"/jobs/{job_id}")
