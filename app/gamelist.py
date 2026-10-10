"""Safe edits of EmulationStation gamelist.xml files.

RetroBat rewrites gamelists when it exits, so it must be fully closed before
these edits; the UI makes the operator confirm that. A timestamped backup is
always written next to the file first.
"""
from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import fsutil


def _norm(path_text: str) -> str:
    p = (path_text or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.casefold()


def _parse(path: Path) -> ET.ElementTree:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.parse(path, parser=parser)


def _load_or_new(path: Path) -> ET.ElementTree:
    if path.exists():
        return _parse(path)
    return ET.ElementTree(ET.Element("gameList"))


@dataclass
class HideChange:
    name: str
    action: str  # add | update | already


def plan_hide(gamelist: Path, names: dict[str, str]) -> list[HideChange]:
    """names: zip file name -> display name for entries that must be hidden."""
    tree = _load_or_new(gamelist)
    by_path = {_norm(g.findtext("path")): g for g in tree.getroot().findall("game")}
    changes = []
    for name in sorted(names, key=str.casefold):
        g = by_path.get(_norm(name))
        if g is None:
            changes.append(HideChange(name, "add"))
        elif (g.findtext("hidden") or "").strip().lower() == "true":
            changes.append(HideChange(name, "already"))
        else:
            changes.append(HideChange(name, "update"))
    return changes


def backup(gamelist: Path) -> Path | None:
    if not gamelist.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = gamelist.with_name(f"{gamelist.name}.bak-{stamp}")
    n = 1
    while dest.exists():
        n += 1
        dest = gamelist.with_name(f"{gamelist.name}.bak-{stamp}-{n}")
    shutil.copy2(gamelist, dest)
    fsutil.fix_perms(dest)
    return dest


def apply_hide(gamelist: Path, names: dict[str, str]) -> tuple[Path | None, list[HideChange]]:
    """Mark entries hidden (adding <game> entries where missing). Returns (backup, changes)."""
    gamelist = Path(gamelist)
    bak = backup(gamelist)
    tree = _load_or_new(gamelist)
    root = tree.getroot()
    by_path = {_norm(g.findtext("path")): g for g in root.findall("game")}
    changes = []
    for name in sorted(names, key=str.casefold):
        g = by_path.get(_norm(name))
        if g is None:
            g = ET.SubElement(root, "game")
            ET.SubElement(g, "path").text = f"./{name}"
            ET.SubElement(g, "name").text = names[name] or Path(name).stem
            ET.SubElement(g, "hidden").text = "true"
            by_path[_norm(name)] = g
            changes.append(HideChange(name, "add"))
            continue
        h = g.find("hidden")
        if h is not None and (h.text or "").strip().lower() == "true":
            changes.append(HideChange(name, "already"))
            continue
        if h is None:
            h = ET.SubElement(g, "hidden")
        h.text = "true"
        changes.append(HideChange(name, "update"))
    ET.indent(tree, space="\t")
    tmp = gamelist.with_name(f".{gamelist.name}.tmp")
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, gamelist)
    fsutil.fix_perms(gamelist)
    return bak, changes
