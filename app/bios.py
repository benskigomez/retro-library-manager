"""BIOS catalog."""
from __future__ import annotations

import yaml
from pydantic import BaseModel, Field


class BiosEntry(BaseModel):
    system: str
    file: str
    md5: str = ""
    required: bool = False
    note: str = ""


class CatalogFile(BaseModel):
    bios: list[BiosEntry] = Field(default_factory=list)


def parse_catalog(text: str) -> list[BiosEntry]:
    return CatalogFile.model_validate(yaml.safe_load(text) or {}).bios
