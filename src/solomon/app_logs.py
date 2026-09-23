"""Generic app-log knowledge ingestion -- the non-Minecraft counterpart to
crash_logs.py. Confirmed against a real Flutter app's `.flutter_run.log`:
unlike Minecraft's one-file-per-crash structured reports, Flutter (and
most app run logs) write one continuous, unstructured log per run --
freeform console text, no `Time:`/`Description:` header to parse
reliably. So this doesn't try to detect/parse individual "crashes";
it ingests the tail of the log file as one searchable note, tagged
`source: app_log`. When an exception did occur, it's just... in that
text, findable via the same term-frequency search as everything else
(searching "exception" or "error" surfaces it) -- no fragile
Flutter-specific stack-trace parser guessed without a real example to
validate it against.
"""

from __future__ import annotations

from pathlib import Path

from .knowledge import Note

_DEFAULT_MAX_LINES = 300


def ingest_app_log(log_path: str | Path, max_lines: int = _DEFAULT_MAX_LINES) -> Note | None:
    path = Path(log_path)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = text.splitlines()
    tail = lines[-max_lines:]
    body = "\n".join(tail)
    truncated_note = "" if len(lines) <= max_lines else f"(showing last {max_lines} of {len(lines)} lines)\n\n"

    return Note(
        path=path,
        title=f"App Log: {path.name}",
        frontmatter={"sao_authority": "current", "source": "app_log"},
        body=truncated_note + body,
    )
