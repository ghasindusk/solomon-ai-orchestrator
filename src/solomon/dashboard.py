"""Dashboard -- lean Phase 6 slice (TUI/Dashboard).

A plain-text snapshot, not a curses-based interactive TUI: curses isn't
in the Windows standard library (would need the extra `windows-curses`
dependency for this Windows-only environment), so this stays
dependency-free instead. `solomon dashboard` prints one snapshot;
`--watch SECONDS` reprints on a loop. Every figure here is read directly
from StateStore/registry/adapter health checks or computed from them --
nothing is invented to fill a panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import AdapterHealth
from .knowledge import estimate_notes_tokens, load_notes_for_project
from .registry import ProjectEntry, ProjectRegistry
from .state import StateStore
from .telemetry import get_gpu_telemetry
from .usage import UsageManager

_ACTIVE_STATUSES = ["QUEUED", "READY", "ASSIGNED", "RUNNING"]
_COMPLETE_STATUSES = ("COMPLETE", "RESULT_RECEIVED")
_FAILED_STATUSES = ("FAILED", "BLOCKED", "REMEDIATION_REQUIRED", "UNKNOWN")


@dataclass
class ProjectSnapshot:
    project: ProjectEntry
    total_tasks: int
    complete_tasks: int
    active_tasks: int
    failed_tasks: int
    progress_pct: float | None  # None -- not 0 -- when there's no recorded task history at all
    usage: dict
    pending_approvals: int
    knowledge_notes: int
    knowledge_raw_tokens_estimated: int


@dataclass
class AgentSnapshot:
    name: str
    available: bool
    detail: str
    stats: dict  # StateStore.get_adapter_stats: count / success_rate / avg_duration_seconds


@dataclass
class DashboardData:
    projects: list[ProjectSnapshot] = field(default_factory=list)
    agents: list[AgentSnapshot] = field(default_factory=list)
    task_queue: list[dict] = field(default_factory=list)
    pending_approvals_total: int = 0
    gpu: dict | None = None  # live snapshot, not historical -- see telemetry.py


def _project_task_counts(store: StateStore, project_id: str) -> tuple[int, int, int, int, float | None]:
    tasks = store.list_tasks(project_id=project_id)
    total = len(tasks)
    complete = sum(1 for t in tasks if t["status"] in _COMPLETE_STATUSES)
    active = sum(1 for t in tasks if t["status"] in _ACTIVE_STATUSES)
    failed = sum(1 for t in tasks if t["status"] in _FAILED_STATUSES)
    pct = (complete / total * 100) if total else None
    return total, complete, active, failed, pct


def build_dashboard(
    store: StateStore,
    registry: ProjectRegistry,
    adapter_health_checks: dict[str, AdapterHealth],
    project_id: str | None = None,
    gpu_telemetry: dict | None = None,
) -> DashboardData:
    """gpu_telemetry, like adapter_health_checks, is gathered by the
    caller (get_gpu_telemetry()) and passed in rather than fetched here
    -- keeps this function testable without touching real hardware."""
    manager = UsageManager(store)
    data = DashboardData(gpu=gpu_telemetry)

    if project_id:
        entry = registry.get(project_id)
        projects = [entry] if entry else []
    else:
        projects = registry.list_projects()

    for project in projects:
        total, complete, active, failed, pct = _project_task_counts(store, project.project_id)
        pending = len(store.list_approval_requests(project_id=project.project_id, status="pending"))
        notes = load_notes_for_project(project)
        data.projects.append(
            ProjectSnapshot(
                project=project,
                total_tasks=total,
                complete_tasks=complete,
                active_tasks=active,
                failed_tasks=failed,
                progress_pct=pct,
                usage=manager.get_project_usage(project.project_id),
                pending_approvals=pending,
                knowledge_notes=len(notes),
                knowledge_raw_tokens_estimated=estimate_notes_tokens(notes),
            )
        )

    for name, health in adapter_health_checks.items():
        data.agents.append(
            AgentSnapshot(
                name=name,
                available=health.available,
                detail=health.detail,
                stats=store.get_adapter_stats(name, project_id=project_id),
            )
        )

    data.task_queue = store.list_tasks(project_id=project_id, statuses=_ACTIVE_STATUSES)
    data.pending_approvals_total = len(store.list_approval_requests(status="pending"))
    return data


def render_dashboard(data: DashboardData) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("SOLOMON AI ORCHESTRATOR -- DASHBOARD")
    lines.append("=" * 70)

    lines.append("\n-- Projects --")
    if not data.projects:
        lines.append("  (none)")
    for snap in data.projects:
        pct_str = f"{snap.progress_pct:.0f}%" if snap.progress_pct is not None else "no data"
        lines.append(f"  {snap.project.project_id} [{snap.project.status}]  progress: {pct_str}")
        lines.append(
            f"    tasks: {snap.total_tasks} total, {snap.complete_tasks} complete, "
            f"{snap.active_tasks} active, {snap.failed_tasks} failed"
        )
        cost = snap.usage["total_cost_usd"]
        cost_str = f"${cost:.4f}" if cost is not None else f"unknown ({snap.usage['cost_provenance']})"
        lines.append(f"    usage: {cost_str}, tokens in={snap.usage['total_input_tokens']} "
                     f"out={snap.usage['total_output_tokens']}")
        lines.append(f"    knowledge: {snap.knowledge_notes} notes, "
                     f"~{snap.knowledge_raw_tokens_estimated} raw tokens (ESTIMATED, unfiltered)")
        if snap.pending_approvals:
            lines.append(f"    ** {snap.pending_approvals} pending approval(s) -- run `approvals list` **")

    lines.append("\n-- Agents --")
    for agent in data.agents:
        status = "UP" if agent.available else "DOWN"
        stats = agent.stats
        sr = f"{stats['success_rate']:.0%}" if stats["success_rate"] is not None else "no data"
        avg = f"{stats['avg_duration_seconds']:.1f}s" if stats["avg_duration_seconds"] is not None else "no data"
        lines.append(f"  {agent.name:16s} [{status}]  runs={stats['count']}  success={sr}  avg={avg}")
        if not agent.available and agent.detail:
            lines.append(f"    detail: {agent.detail}")

    lines.append(f"\n-- Task Queue ({len(data.task_queue)} active) --")
    for task in data.task_queue[:20]:
        lines.append(f"  {task['task_id']}  [{task['status']}]  role={task['role']}  project={task['project_id']}")
    if len(data.task_queue) > 20:
        lines.append(f"  ... and {len(data.task_queue) - 20} more")

    lines.append(f"\n-- Approvals: {data.pending_approvals_total} pending total --")

    lines.append("\n-- Local Hardware (live snapshot, not historical) --")
    if data.gpu:
        lines.append(
            f"  GPU: {data.gpu['name']}  load={data.gpu['gpu_load_percent']:.0f}%  "
            f"VRAM={data.gpu['vram_used_mb']:.0f}/{data.gpu['vram_total_mb']:.0f} MB"
        )
    else:
        lines.append("  GPU: unavailable (nvidia-smi not found or not queried)")

    lines.append("=" * 70)
    return "\n".join(lines)
