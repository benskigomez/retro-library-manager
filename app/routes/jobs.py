"""Job history, detail, live log (SSE) and cancel."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..jobs import TERMINAL
from . import redirect, render, st

router = APIRouter()


@router.get("/jobs")
def job_list(request: Request):
    s = st(request)
    return render(request, "jobs.html", jobs=s.queue.recent(300))


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int):
    s = st(request)
    job = s.queue.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return render(request, "job.html", job=job, items=s.queue.items(job_id), terminal=job["status"] in TERMINAL)


@router.get("/jobs/{job_id}/status")
def job_status(request: Request, job_id: int):
    s = st(request)
    job = s.queue.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return render(request, "partials/job_status.html", job=job, terminal=job["status"] in TERMINAL)


@router.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: int):
    st(request).queue.cancel(job_id)
    return redirect(request, f"/jobs/{job_id}")


@router.get("/jobs/{job_id}/log")
def job_log(request: Request, job_id: int):
    path = st(request).queue.log_path(job_id)
    if not path.exists():
        raise HTTPException(404, "no log yet")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: int):
    """Server-Sent Events: the job's log file as it grows, then a final 'end' event."""
    s = st(request)
    if not s.queue.get(job_id):
        raise HTTPException(404, "no such job")
    path = s.queue.log_path(job_id)

    async def events():
        pos = 0
        pending = ""
        while True:
            if await request.is_disconnected():
                return
            chunk = ""
            if path.exists():
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read(256 * 1024)
                    pos = f.tell()
            if chunk:
                pending += chunk
                lines = pending.split("\n")
                pending = lines.pop()
                if lines:
                    yield f"data: {json.dumps(lines)}\n\n"
                continue
            job = s.queue.get(job_id)
            if job and job["status"] in TERMINAL:
                if pending:
                    yield f"data: {json.dumps([pending])}\n\n"
                yield f"event: end\ndata: {json.dumps(job['status'])}\n\n"
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
