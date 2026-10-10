"""HTTP basic auth with a PBKDF2 password hash stored in config.yaml."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

from fastapi import HTTPException, Request, status

ITERATIONS = 200_000

# PBKDF2 is deliberately slow; remember headers that already verified against
# the current hash so HTMX polling and SSE do not pay for it on every request.
_VERIFIED: set[str] = set()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    return hmac.compare_digest(dk.hex(), digest)


def set_hash_in_file(config_path: Path, new_hash: str) -> None:
    """Replace the password_hash line in place so the file keeps its comments."""
    text = config_path.read_text(encoding="utf-8")
    new_text, n = re.subn(r'(?m)^(\s*password_hash:\s*).*$', lambda m: f'{m.group(1)}"{new_hash}"', text, count=1)
    if n == 0:
        raise ValueError("config.yaml has no auth.password_hash line")
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, config_path)


def bootstrap_password(settings, log=print) -> None:
    """First run: take RLM_PASSWORD, or generate one and print it to the container log."""
    cfg = settings.config
    if os.environ.get("RLM_USERNAME"):
        cfg.auth.username = os.environ["RLM_USERNAME"]
    if cfg.auth.password_hash:
        return
    pw = os.environ.get("RLM_PASSWORD")
    if not pw:
        pw = secrets.token_urlsafe(12)
        log("=" * 60)
        log(f"Retro Library Manager: generated login  {cfg.auth.username} / {pw}")
        log("Change it on the Settings page.")
        log("=" * 60)
    h = hash_password(pw)
    set_hash_in_file(settings.config_path, h)
    settings.reload()
    if os.environ.get("RLM_USERNAME"):
        settings.config.auth.username = os.environ["RLM_USERNAME"]


def require_auth(request: Request) -> str:
    settings = request.app.state.settings
    auth = settings.config.auth
    header = request.headers.get("authorization", "")
    cache_key = hashlib.sha256((header + "\0" + auth.password_hash + "\0" + auth.username).encode()).hexdigest()
    if cache_key in _VERIFIED:
        return auth.username
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            user, pw = "", ""
        if hmac.compare_digest(user, auth.username) and verify_password(pw, auth.password_hash):
            if len(_VERIFIED) > 64:
                _VERIFIED.clear()
            _VERIFIED.add(cache_key)
            return user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="Retro Library Manager"'},
    )
