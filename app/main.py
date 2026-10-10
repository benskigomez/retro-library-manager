"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .auth import bootstrap_password, require_auth
from .config import Settings
from .state import State

HERE = Path(__file__).parent
log = logging.getLogger("rlm")


def human_size(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(HERE / "templates"))
    t.env.filters["size"] = human_size
    t.env.globals["version"] = __version__
    t.env.globals["now"] = datetime.now
    return t


def create_app(settings: Settings | None = None, start_workers: bool = True) -> FastAPI:
    settings = settings or Settings()
    bootstrap_password(settings, log=lambda m: print(m, flush=True))
    state = State(settings)

    from .handlers import register_handlers

    register_handlers(state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_workers:
            state.queue.start()
        yield
        state.queue.stop()

    app = FastAPI(title="Retro Library Manager", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.rlm = state
    app.state.templates = make_templates()
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    from .routes import arcade, bios, chd, dashboard, extract, files, jobs, settings_page, trash

    deps = [Depends(require_auth)]
    for module in (dashboard, jobs, settings_page, trash, files, extract, chd, bios, arcade):
        app.include_router(module.router, dependencies=deps)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.exception_handler(PermissionError)
    async def perm_error(request: Request, exc: PermissionError):
        return HTMLResponse(f'<div class="flash error">{exc}</div>', status_code=403)

    return app
