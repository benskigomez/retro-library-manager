"""System-specific post-processing hooks, run after extraction."""
from __future__ import annotations

from . import lynx

HOOKS = {
    "lynx": lynx.run,
}
