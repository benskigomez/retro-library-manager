"""Settings: YAML editors, password change, per-system filter rules."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

from ..auth import hash_password, verify_password
from ..bios import parse_catalog
from ..config import load_yaml_config
from ..filters import RuleSet
from ..systems import parse_systems
from . import redirect, render, st

router = APIRouter()

FILES = {
    "config": ("config.yaml", "settings_path_config"),
    "systems": ("systems.yaml", "settings_path_systems"),
    "bios_catalog": ("bios_catalog.yaml", "settings_path_bios"),
}


def _path(s, which: str):
    return {
        "config": s.settings.config_path,
        "systems": s.settings.systems_path,
        "bios_catalog": s.settings.bios_catalog_path,
    }[which]


VALIDATORS = {
    "config": load_yaml_config,
    "systems": parse_systems,
    "bios_catalog": parse_catalog,
}


@router.get("/settings")
def settings_home(request: Request, which: str = "config"):
    s = st(request)
    if which not in FILES:
        raise HTTPException(404)
    text = _path(s, which).read_text(encoding="utf-8")
    return render(request, "settings.html", which=which, text=text, files=FILES, error=None,
                  saved=request.query_params.get("saved"))


@router.post("/settings/{which}")
def settings_save(request: Request, which: str, text: str = Form(...)):
    s = st(request)
    if which not in FILES:
        raise HTTPException(404)
    try:
        s.settings.save_text(_path(s, which), text.replace("\r\n", "\n"), VALIDATORS[which])
    except Exception as e:  # noqa: BLE001 - show validation errors to the operator
        return render(request, "settings.html", which=which, text=text, files=FILES, error=str(e), saved=None)
    s.reload()
    return redirect(request, f"/settings?which={which}&saved=1")


@router.post("/settings/password/change")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    s = st(request)
    if not verify_password(current, s.cfg.auth.password_hash):
        return render(request, "partials/flash.html", kind="error", message="Current password is wrong.")
    if new != confirm or len(new) < 8:
        return render(request, "partials/flash.html", kind="error",
                      message="New passwords must match and be at least 8 characters.")
    h = hash_password(new)
    s.settings.update_config(lambda d: d.setdefault("auth", {}).__setitem__("password_hash", h))
    s.reload()
    return render(request, "partials/flash.html", kind="ok",
                  message="Password changed. Your browser will ask you to log in again.")


# ---- per-system filters --------------------------------------------------------

def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]


@router.get("/settings/filters/{key}")
def filters_page(request: Request, key: str):
    s = st(request)
    s.system(key)
    o = s.cfg.filters.systems.get(key)
    return render(request, "filters.html", key=key, o=o, g=s.cfg.filters, result=None)


@router.post("/settings/filters/{key}")
def filters_save(request: Request, key: str,
                 override_include: str = Form(""), include: str = Form(""),
                 override_exclude: str = Form(""), exclude: str = Form(""),
                 allow: str = Form(""), deny: str = Form("")):
    s = st(request)
    s.system(key)
    entry: dict = {}
    if override_include:
        entry["include"] = _lines(include)
    if override_exclude:
        entry["exclude"] = _lines(exclude)
    if _lines(allow):
        entry["allow"] = _lines(allow)
    if _lines(deny):
        entry["deny"] = _lines(deny)

    def mutate(d):
        f = d.setdefault("filters", {})
        systems = f.get("systems") or {}
        if entry:
            systems[key] = entry
        else:
            systems.pop(key, None)
        f["systems"] = systems

    try:
        s.settings.update_config(mutate)
    except Exception as e:  # noqa: BLE001
        return render(request, "partials/flash.html", kind="error", message=f"Not saved: {e}")
    s.reload()
    return redirect(request, f"/settings/filters/{key}?saved=1")


@router.post("/settings/filters/{key}/test")
def filters_test(request: Request, key: str, names: str = Form("")):
    s = st(request)
    rules = RuleSet(s.cfg.filters, key)
    sysdef = s.system(key)
    rows = [(n, rules.decide_arcade(n) if sysdef.arcade else rules.decide(n)) for n in _lines(names)]
    return render(request, "partials/filter_test.html", rows=rows)
