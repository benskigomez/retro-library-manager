"""System definitions (systems.yaml)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

ARCHIVE_EXTS = {".zip", ".7z"}


class System(BaseModel):
    key: str = ""
    name: str
    extensions: list[str] = Field(default_factory=list)
    disc: Literal["cd", "dvd"] | None = None
    arcade: bool = False
    hooks: list[str] = Field(default_factory=list)

    @field_validator("extensions")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        return [e.lower() if e.startswith(".") else "." + e.lower() for e in v]

    @property
    def rom_extensions(self) -> set[str]:
        return set(self.extensions)


class SystemsFile(BaseModel):
    systems: dict[str, System]


def parse_systems(text: str) -> dict[str, System]:
    data = yaml.safe_load(text) or {}
    parsed = SystemsFile.model_validate(data)
    for key, sysdef in parsed.systems.items():
        sysdef.key = key
    return parsed.systems


def load_systems(path: Path) -> dict[str, System]:
    return parse_systems(path.read_text(encoding="utf-8"))


def detect_system(names: list[str], systems: dict[str, System]) -> list[tuple[str, int]]:
    """Rank systems by how many entry names carry one of their extensions.

    Extensions shared by many systems (.bin, .iso, .zip ...) are weighted down so
    that a pile of .pce files is not confused with something generic.
    """
    ext_counts = Counter(Path(n).suffix.lower() for n in names if not n.endswith("/"))
    owners: Counter[str] = Counter()
    for s in systems.values():
        for e in s.rom_extensions:
            owners[e] += 1
    scores: Counter[str] = Counter()
    for key, s in systems.items():
        for ext, cnt in ext_counts.items():
            if ext in s.rom_extensions:
                scores[key] += cnt * 100 // owners[ext]
    return [(k, v) for k, v in scores.most_common() if v > 0]
