"""Region / quality filter rules.

Rules are always matched against the entry's base filename, never its folder
path, so "GB/[BIOS] ... .gb" is recognised as a BIOS and a folder called
"(Proto)" does not exclude everything inside it.

Precedence: BIOS prefix > deny-list > allow-list > exclude > include > default exclude.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .config import FiltersCfg

INCLUDE = "include"
EXCLUDE = "exclude"
BIOS = "bios"


@dataclass(frozen=True)
class Decision:
    category: str  # include | exclude | bios
    rule: str      # human-readable rule that decided it


def basename(entry: str) -> str:
    return entry.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


@lru_cache(maxsize=512)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _title_match(spec: str, name: str) -> bool:
    if spec.startswith("re:"):
        return _rx("(?i)" + spec[3:]).search(name) is not None
    return spec.casefold() in name.casefold()


class RuleSet:
    def __init__(self, cfg: FiltersCfg, system: str | None = None):
        override = cfg.systems.get(system) if system else None
        self.bios_prefix = cfg.bios_prefix
        self.include = override.include if override and override.include is not None else cfg.include
        self.exclude = override.exclude if override and override.exclude is not None else cfg.exclude
        self.allow = list(override.allow) if override else []
        self.deny = list(override.deny) if override else []
        self.scope = f"{system} override" if override else "global"

    def decide(self, entry: str) -> Decision:
        name = basename(entry)
        if self.bios_prefix and name.startswith(self.bios_prefix):
            return Decision(BIOS, f"name starts with {self.bios_prefix}")
        for spec in self.deny:
            if _title_match(spec, name):
                return Decision(EXCLUDE, f"deny-list: {spec}")
        for spec in self.allow:
            if _title_match(spec, name):
                return Decision(INCLUDE, f"allow-list: {spec}")
        for pat in self.exclude:
            m = _rx(pat).search(name)
            if m:
                return Decision(EXCLUDE, f"exclude {m.group(0)}")
        for pat in self.include:
            m = _rx(pat).search(name)
            if m:
                return Decision(INCLUDE, f"region {m.group(0)}")
        return Decision(EXCLUDE, "no USA/World/English region tag")

    def decide_arcade(self, entry: str) -> Decision:
        """Arcade sets use MAME short names; only deny/allow lists apply."""
        name = basename(entry)
        for spec in self.deny:
            if _title_match(spec, name):
                return Decision(EXCLUDE, f"deny-list: {spec}")
        return Decision(INCLUDE, "arcade set (no region filter)")
