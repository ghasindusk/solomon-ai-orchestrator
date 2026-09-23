"""Installed-mod inventory -- makes "what's actually installed right now"
queryable, rather than a fabricated diff against documentation.

Considered parsing crash reports' "-- System Details --" > "Mod List"
table (confirmed real, structured: `<jar>|<name>|<modid>|<version>|...`)
for a modid/version-accurate inventory, but that's only as fresh as the
last crash -- could be stale for days if nothing crashed. Scanning the
mods/ directory's jar filenames directly (confirmed real: 177 .jar files
in an actual modded-Minecraft instance) is always current
instead, at the cost of a best-effort filename-only name/version split
(mod jar naming isn't fully standardized, so this is a guess, not
authoritative -- the raw filename is always kept too so nothing is lost
even when the guess is wrong).

This does NOT diff against a canonical "expected mod list" -- none of
the reference modpack projects document one in a reliably parseable form,
and fabricating a comparison against data that doesn't really exist
would be exactly the kind of made-up result this codebase has avoided
throughout. What this gives instead: a real, current, searchable fact
base a debugging Task (or a human) can query ("is embeddium actually
installed, what version") instead of manually running `ls mods/`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .knowledge import Note

_JAR_NAME_RE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[\w.\-+]*)\.jar$")


@dataclass
class ModJar:
    filename: str
    guessed_name: str | None
    guessed_version: str | None


def list_installed_mods(mods_path: str | Path) -> list[ModJar]:
    root = Path(mods_path)
    if not root.exists():
        return []
    mods: list[ModJar] = []
    for jar in sorted(root.glob("*.jar")):
        match = _JAR_NAME_RE.match(jar.name)
        if match:
            mods.append(ModJar(filename=jar.name, guessed_name=match.group("name"), guessed_version=match.group("version")))
        else:
            mods.append(ModJar(filename=jar.name, guessed_name=None, guessed_version=None))
    return mods


def mod_inventory_as_note(mods_path: str | Path) -> Note | None:
    mods = list_installed_mods(mods_path)
    if not mods:
        return None
    lines = [
        f"{m.filename}" + (f"  (guessed: {m.guessed_name} {m.guessed_version})" if m.guessed_name else "")
        for m in mods
    ]
    body = f"{len(mods)} mods installed.\n\n" + "\n".join(lines)
    return Note(
        path=Path(mods_path),
        title=f"Installed Mods ({len(mods)})",
        frontmatter={"sao_authority": "current", "source": "mod_inventory"},
        body=body,
    )
