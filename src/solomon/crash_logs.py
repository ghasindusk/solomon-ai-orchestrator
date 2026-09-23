"""Crash-report knowledge ingestion -- bridges Minecraft gameplay and mod
development: a crash discovered while playing becomes searchable
knowledge Solomon can pull into a debugging Task's context pack,
without a manual copy-paste step.

Minecraft/Forge crash reports (confirmed against real files from a
real modded-Minecraft instance) are huge
(30-100KB+) mostly-noise text files: a `Time:`/`Description:` header,
a Java exception + stack trace (often hundreds of mixin-decorated
frames), then a `-- System Details --` footer repeating the mod list.
Only the header + the first ~20 stack frames are extracted as the
summary -- enough to identify the failing mod/class, not the whole
file. The full file path is kept so a human or a follow-up Task can
open it for the complete trace.

Reuses `knowledge.Note` rather than inventing a parallel data model --
crash reports become synthetic notes tagged `source: crash_report`, so
they flow through the exact same search_notes/build_context_pack
pipeline as real Obsidian notes automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .knowledge import Note

_TIME_RE = re.compile(r"^Time:\s*(.+)$", re.MULTILINE)
_DESCRIPTION_RE = re.compile(r"^Description:\s*(.+)$", re.MULTILINE)
_FILENAME_RE = re.compile(r"^crash-(\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2})-(\w+)\.txt$")
_STACK_FRAME_LIMIT = 20


@dataclass
class CrashReport:
    path: Path
    timestamp: str | None
    flavor: str  # client | server | fml | unknown (from the filename suffix)
    description: str
    summary: str


def find_crash_reports(crash_logs_path: str | Path) -> list[Path]:
    root = Path(crash_logs_path)
    if not root.exists():
        return []
    return sorted(root.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_crash_report(path: Path) -> CrashReport | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    filename_match = _FILENAME_RE.match(path.name)
    timestamp = filename_match.group(1) if filename_match else None
    flavor = filename_match.group(2) if filename_match else "unknown"

    time_match = _TIME_RE.search(text)
    if time_match:
        timestamp = time_match.group(1).strip()

    desc_match = _DESCRIPTION_RE.search(text)
    description = desc_match.group(1).strip() if desc_match else "(no Description line found)"

    if desc_match:
        after_description = text[desc_match.end():]
        lines = [ln for ln in after_description.splitlines() if ln.strip()][:_STACK_FRAME_LIMIT]
        stack_excerpt = "\n".join(lines)
    else:
        stack_excerpt = ""

    summary = f"Description: {description}\n\n{stack_excerpt}".strip()
    return CrashReport(path=path, timestamp=timestamp, flavor=flavor, description=description, summary=summary)


def crash_reports_as_notes(crash_logs_path: str | Path, limit: int = 20) -> list[Note]:
    notes: list[Note] = []
    for path in find_crash_reports(crash_logs_path)[:limit]:
        report = parse_crash_report(path)
        if report is None:
            continue
        title = f"Crash Report: {report.timestamp or path.stem} ({report.flavor})"
        notes.append(
            Note(
                path=path,
                title=title,
                frontmatter={"sao_authority": "current", "source": "crash_report"},
                body=report.summary,
            )
        )
    return notes
