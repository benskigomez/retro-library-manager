from __future__ import annotations

from fastapi import Request

from ..state import State


def st(request: Request) -> State:
    return request.app.state.rlm


def render(request: Request, template: str, **ctx):
    templates = request.app.state.templates
    ctx.setdefault("state", st(request))
    ctx.setdefault("htmx", request.headers.get("hx-request") == "true")
    return templates.TemplateResponse(request, template, ctx)


def redirect(request: Request, url: str):
    """303 for plain forms; HX-Redirect for HTMX so the whole page navigates."""
    from fastapi.responses import RedirectResponse, Response

    if request.headers.get("hx-request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
