"""Replay & Router Comparison (v0.4 Phase 7 Reliability & Evaluation,
DECISIONS.md D31). Formal Spec v0.4 section 18: "Replay: re-run historical
event/task inputs against a new router/policy without mutating the
original record."

Scope (thin slice, D1): this replays a stored Task against Router.route()
using the *current* StateStore-derived history/config, not a point-in-time
reconstruction of what history looked like at the moment the task was
originally routed -- that would need a "stats as of timestamp X" query
this codebase doesn't have, and the spec only asks for comparing a new
router/policy against historical inputs, not exact historical
reproduction. Never calls an adapter, never writes a task/result row --
read + score only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import AdapterHealth
from .models import Task
from .router import AgentScore, Router
from .state import StateStore

__all__ = ["ReplayResult", "replay_task", "RouterComparisonReport", "compare_routers"]


def _original_agent_for_task(store: StateStore, task_id: str) -> str | None:
    results = store.get_task_results(task_id)
    return results[0].get("agent") if results else None


@dataclass
class ReplayResult:
    task_id: str
    original_agent: str | None
    scores: list[AgentScore]

    @property
    def replayed_top_choice(self) -> str | None:
        return self.scores[0].adapter_name if self.scores else None

    @property
    def decision_changed(self) -> bool:
        """None (not True/False) when either side is UNKNOWN -- absence
        of a recorded original agent, or no routable candidates, is not
        evidence of a change."""
        if self.original_agent is None or self.replayed_top_choice is None:
            return None
        return self.original_agent != self.replayed_top_choice


def replay_task(
    task_id: str,
    store: StateStore,
    router: Router,
    health_checks: dict[str, AdapterHealth] | None = None,
    gpu_telemetry: dict | None = None,
) -> ReplayResult:
    """Re-scores a previously stored Task under `router` right now.
    Raises ValueError if no such task exists (never fabricates one)."""
    task_data = store.get_task(task_id)
    if task_data is None:
        raise ValueError(f"no such task: {task_id}")
    task = Task.from_schema_dict(task_data)
    scores = router.route(task, health_checks=health_checks or {}, gpu_telemetry=gpu_telemetry)
    return ReplayResult(
        task_id=task_id,
        original_agent=_original_agent_for_task(store, task_id),
        scores=scores,
    )


@dataclass
class RouterComparisonReport:
    task_count: int = 0
    changed_count: int = 0
    changes: list[dict] = field(default_factory=list)

    @property
    def change_rate(self) -> float | None:
        return (self.changed_count / self.task_count) if self.task_count else None


def compare_routers(
    task_ids: list[str], store: StateStore, router_a: Router, router_b: Router
) -> RouterComparisonReport:
    """Replays every task_id under both routers (neither mutates state)
    and reports where their top choices diverge. router_a is typically
    "current config", router_b an alternate (different weights.yaml,
    different policy) being evaluated before adoption. task_ids that no
    longer exist are silently skipped rather than raising -- a comparison
    run over a broad ID list shouldn't abort on one stale reference."""
    report = RouterComparisonReport()
    for task_id in task_ids:
        task_data = store.get_task(task_id)
        if task_data is None:
            continue
        task = Task.from_schema_dict(task_data)
        scores_a = router_a.route(task)
        scores_b = router_b.route(task)
        top_a = scores_a[0].adapter_name if scores_a else None
        top_b = scores_b[0].adapter_name if scores_b else None
        report.task_count += 1
        if top_a != top_b:
            report.changed_count += 1
            report.changes.append(
                {
                    "task_id": task_id,
                    "original": _original_agent_for_task(store, task_id),
                    "router_a": top_a,
                    "router_b": top_b,
                }
            )
    return report
