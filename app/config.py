"""Configuration: a YAML file under /roms/_manager, validated with pydantic.

The shipped defaults live in app/data/. On first run they are copied next to the
database so they are covered by the share's backup and editable in the app.
"""
from __future__ import annotations

import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

DATA_DIR = Path(__file__).parent / "data"


class PathsCfg(BaseModel):
    roms: str = "/roms"
    temp: str = "/tmp/rlm"
    desktop_bios: str = ""
    browse_roots: list[str] = Field(default_factory=list)


class AuthCfg(BaseModel):
    username: str = "admin"
    password_hash: str = ""


class JobsCfg(BaseModel):
    workers: int = Field(1, ge=1, le=8)


class StabilityCfg(BaseModel):
    wait_seconds: float = Field(5, ge=0, le=300)


class OwnershipCfg(BaseModel):
    apply: bool = True
    uid: int = 99
    gid: int = 100


class ToolsCfg(BaseModel):
    sevenzip: str = "7zz"
    chdman: str = "chdman"
    unzip: str = "unzip"
    zip_backend: Literal["python", "unzip"] = "python"


class SystemFilterCfg(BaseModel):
    include: list[str] | None = None
    exclude: list[str] | None = None
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class FiltersCfg(BaseModel):
    bios_prefix: str = "[BIOS]"
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    systems: dict[str, SystemFilterCfg] = Field(default_factory=dict)

    @field_validator("include", "exclude")
    @classmethod
    def _compile(cls, v: list[str]) -> list[str]:
        import re

        for pat in v:
            re.compile(pat)
        return v


class BiosCfg(BaseModel):
    share_unc: str = r"\\jarvis\roms"
    desktop_path: str = r"G:\RetroBat\bios"


class LynxCfg(BaseModel):
    manufacturer: str = ""
    rotation: dict[str, Literal["left", "right", "none"]] = Field(default_factory=dict)


class Config(BaseModel):
    paths: PathsCfg = Field(default_factory=PathsCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)
    jobs: JobsCfg = Field(default_factory=JobsCfg)
    stability: StabilityCfg = Field(default_factory=StabilityCfg)
    ownership: OwnershipCfg = Field(default_factory=OwnershipCfg)
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    ignore_extensions: list[str] = Field(default_factory=lambda: [".txt", ".nfo"])
    filters: FiltersCfg = Field(default_factory=FiltersCfg)
    bios: BiosCfg = Field(default_factory=BiosCfg)
    lynx: LynxCfg = Field(default_factory=LynxCfg)

    # ---- derived paths -------------------------------------------------
    @property
    def roms(self) -> Path:
        return Path(self.paths.roms)

    @property
    def manager_dir(self) -> Path:
        return self.roms / "_manager"

    @property
    def source_dir(self) -> Path:
        return self.roms / "_source"

    @property
    def bios_dir(self) -> Path:
        return self.roms / "_bios"

    @property
    def trash_dir(self) -> Path:
        return self.roms / "_trash"

    @property
    def logs_dir(self) -> Path:
        return self.manager_dir / "logs"

    @property
    def temp_dir(self) -> Path:
        return Path(self.paths.temp)

    def system_dir(self, system: str) -> Path:
        return self.roms / system


def load_yaml_config(text: str) -> Config:
    data = yaml.safe_load(text) or {}
    return Config.model_validate(data)


class Settings:
    """Holds the live config and the paths of the editable YAML files."""

    def __init__(self, roms: str | None = None, config_path: str | None = None):
        roms = roms or os.environ.get("RLM_ROMS", "/roms")
        manager = Path(roms) / "_manager"
        self.config_path = Path(config_path or os.environ.get("RLM_CONFIG", manager / "config.yaml"))
        self.systems_path = manager / "systems.yaml"
        self.bios_catalog_path = manager / "bios_catalog.yaml"
        self._lock = threading.RLock()
        self._seed(manager)
        self.config = self._read_config(roms)

    def _seed(self, manager: Path) -> None:
        manager.mkdir(parents=True, exist_ok=True)
        for src, dst in (
            (DATA_DIR / "config.default.yaml", self.config_path),
            (DATA_DIR / "systems.yaml", self.systems_path),
            (DATA_DIR / "bios_catalog.yaml", self.bios_catalog_path),
        ):
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)

    def _read_config(self, roms: str) -> Config:
        cfg = load_yaml_config(self.config_path.read_text(encoding="utf-8"))
        # RLM_ROMS / the constructor argument always wins: it is where we found the file.
        cfg.paths.roms = str(roms)
        return cfg

    def reload(self) -> None:
        with self._lock:
            self.config = self._read_config(self.config.paths.roms)

    def save_text(self, path: Path, text: str, validate) -> None:
        """Validate then write a YAML file, keeping a timestamped backup."""
        validate(text)
        with self._lock:
            if path.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copyfile(path, path.with_name(f"{path.name}.bak-{stamp}"))
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            self.reload()

    def update_config(self, mutate) -> None:
        """Round-trip edit of config.yaml that keeps comments and ordering.

        ``mutate`` receives the ruamel CommentedMap and changes it in place.
        """
        import io

        from ruamel.yaml import YAML

        ry = YAML()
        ry.preserve_quotes = True
        ry.width = 4096
        with self._lock:
            data = ry.load(self.config_path.read_text(encoding="utf-8")) or {}
            mutate(data)
            buf = io.StringIO()
            ry.dump(data, buf)
            self.save_text(self.config_path, buf.getvalue(), load_yaml_config)
