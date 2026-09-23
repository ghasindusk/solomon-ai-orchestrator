"""Diagnostics Export (v0.4 Phase 8 UX, DECISIONS.md D33). Formal Spec
v0.4 section 21: "Diagnostics exports must redact/omit prompts, source
contents, secrets, private paths and Obsidian contents unless the user
explicitly includes them."

Reuses existing data sources (StateStore, ProjectRegistry, adapter health
checks, GPU telemetry, UsageManager) the same way dashboard.py does,
rather than a parallel data-access layer -- this is a redaction +
serialization pass over what `solomon dashboard`/`usage`/`approvals
list`/`learning-report` already read, not a new query surface.

Two modes:
- Default (include_sensitive=False): aggregate counts/statuses/
  timestamps/IDs/numeric usage/hardware telemetry only. No prompt text,
  no approval reason/task_params, no event `detail` free text, no
  project filesystem paths (repo_path/knowledge_path/crash_logs_path/
  mods_path/app_log_path/build_command), no knowledge-note titles or
  content.
- include_sensitive=True ("unless the user explicitly includes them"):
  additionally includes approval reason/task_params, event detail, and
  project filesystem paths and note titles -- but every free-text
  string still passes through knowledge.redact_secrets() first, since
  an explicitly-included prompt or path could still contain a real
  credential the user pasted by accident. Note BODIES are never
  included in either mode -- a diagnostics bundle exists to debug
  Solomon's behavior, not to export vault content; that's what
  export-log/export-events-json or the vault itself are for.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .adapters.base import AdapterHealth
from .dashboard import _project_task_counts
from .knowledge import estimate_notes_tokens, load_notes_for_project, redact_secrets
from .registry import ProjectRegistry
from .state import StateStore
from .usage import UsageManager

__all__ = ["DiagnosticsReport", "build_diagnostics_report"]

_PRIVATE_PATH_FIELDS = (
    "repo_path",
    "knowledge_path",
    "crash_logs_path",
    "mods_path",
    "app_log_path",
    "build_command",
)


def _redact_task_params(task_params: dict) -> dict:
    """task_params (approval_requests) carries the raw prompt and other
    adapter invocation details -- redact every string value defensively,
    not just the known "prompt" key, since adapters/CLI args can add new
    string fields over time without this module knowing about them."""
    redacted = {}
    for key, value in task_params.items():
        if isinstance(value, str):
            text, _ = redact_secrets(value)
            redacted[key] = text
        else:
            redacted[key] = value
    return redacted


@dataclass
class DiagnosticsReport:
    generated_at: str
    include_sensitive: bool
    environment: dict = field(default_factory=dict)
    projects: list[dict] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    approvals_summary: dict = field(default_factory=dict)
    approvals: list[dict] | None = None
    recent_events: list[dict] = field(default_factory=list)
    gpu: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_diagnostics_report(
    store: StateStore,
    registry: ProjectRegistry,
    adapter_health_checks: dict[str, AdapterHealth],
    *,
    project_id: str | None = None,
    gpu_telemetry: dict | None = None,
    include_sensitive: bool = False,
    event_limit: int = 50,
) -> DiagnosticsReport:
    """Like dashboard.build_dashboard, adapter_health_checks/gpu_telemetry
    are gathered by the caller and passed in -- keeps this testable
    without touching real hardware or spawning adapter subprocesses."""
    manager = UsageManager(store)
    report = DiagnosticsReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        include_sensitive=include_sensitive,
        environment={
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        gpu=gpu_telemetry,
    )

    if project_id:
        entry = registry.get(project_id)
        projects = [entry] if entry else []
    else:
        projects = registry.list_projects()

    for project in projects:
        total, complete, active, failed, pct = _project_task_counts(store, project.project_id)
        notes = load_notes_for_project(project)
        project_entry = {
            "project_id": project.project_id,
            "name": project.name,
            "status": project.status,
            "tags": list(project.tags),
            "total_tasks": total,
            "complete_tasks": complete,
            "active_tasks": active,
            "failed_tasks": failed,
            "progress_pct": pct,
            "usage": manager.get_project_usage(project.project_id),
            "knowledge_notes": len(notes),
            "knowledge_raw_tokens_estimated": estimate_notes_tokens(notes),
        }
        if include_sensitive:
            for field_name in _PRIVATE_PATH_FIELDS:
                value = getattr(project, field_name)
                project_entry[field_name] = redact_secrets(value)[0] if value else value
            project_entry["knowledge_note_titles"] = [n.title for n in notes]
        report.projects.append(project_entry)

    for name, health in adapter_health_checks.items():
        detail_text, _ = redact_secrets(health.detail or "")
        report.agents.append({"name": name, "available": health.available, "detail": detail_text})

    approvals = store.list_approval_requests(project_id=project_id)
    summary: dict[str, int] = {}
    for a in approvals:
        key = f"{a['status']}:{a['risk']}"
        summary[key] = summary.get(key, 0) + 1
    report.approvals_summary = summary

    if include_sensitive:
        report.approvals = [
            {
                "request_id": a["request_id"],
                "project_id": a["project_id"],
                "status": a["status"],
                "risk": a["risk"],
                "reason": redact_secrets(a["reason"])[0],
                "task_params": _redact_task_params(a["task_params"]),
                "requested_at": a["requested_at"],
            }
            for a in approvals
        ]

    events = store.list_events(project_id=project_id)[-event_limit:]
    for e in events:
        entry = {"ts": e["ts"], "event": e["event"], "task_id": e["task_id"], "agent": e["agent"]}
        if include_sensitive and e.get("detail"):
            entry["detail"] = redact_secrets(e["detail"])[0]
        report.recent_events.append(entry)

    return report
