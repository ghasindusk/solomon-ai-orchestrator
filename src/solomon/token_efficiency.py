"""Token Efficiency (v0.4 Token & Compute Intelligence, Phase 6 reopen step
6/10 -- DECISIONS.md D27). Additional spec section 8 / Formal Spec v0.4
section 17.6: an auxiliary Router signal (tokens_per_task /
tokens_per_success / tokens_per_verified_success / retry_token_overhead).

"It must not mechanically select the lowest-token agent; efficiency is
compared only among candidates that already satisfy Safety Policy,
required capability, quality and project experience" (Formal Spec section
17.6) -- this module only computes the numbers. Wiring them into the
Router as one input among several, downstream of the hard safety/
capability gates, is step 7.
"""

from __future__ import annotations

from dataclasses import dataclass

from .usage_record import UsageRecord

__all__ = ["TokenEfficiency", "compute_token_efficiency"]


@dataclass
class TokenEfficiency:
    scope: str
    scope_id: str | None
    task_count: int = 0
    success_task_count: int = 0
    verified_success_task_count: int = 0
    retried_task_count: int = 0
    tokens_per_task: float | None = None
    tokens_per_success: float | None = None
    tokens_per_verified_success: float | None = None
    retry_token_overhead: float | None = None


def compute_token_efficiency(
    records: list[UsageRecord],
    *,
    success_task_ids: set[str],
    verified_task_ids: set[str],
    scope: str = "global",
    scope_id: str | None = None,
) -> TokenEfficiency:
    """Aggregate per-task token totals from `records` (typically
    StateStore.get_usage_records(), one or more UsageRecords per task_id --
    more than one means the task was retried) into the four Token
    Efficiency metrics.

    A UsageRecord carries no outcome of its own (Formal Spec section 6:
    agent output is evidence, not completion). Callers derive
    `success_task_ids`/`verified_task_ids` from Task.status via
    StateStore.list_tasks(), e.g.:
        success_task_ids = {t["task_id"] for t in
            store.list_tasks(project_id=pid, statuses=["RESULT_RECEIVED", "COMPLETE"])}
        verified_task_ids = {t["task_id"] for t in
            store.list_tasks(project_id=pid, statuses=["COMPLETE"])}
    "success" = the agent's result was accepted as evidence (RESULT_RECEIVED
    or further along); "verified success" = Task.status == COMPLETE, i.e.
    passed Definition of Done verification (D4/section 14) -- a strictly
    narrower set.

    retry_token_overhead is averaged only over tasks that actually had more
    than one UsageRecord (multiple attempts), as (total tokens across all
    attempts) - (first attempt's tokens); records for one task_id must
    already be in recorded order (StateStore.get_usage_records() preserves
    task_results insertion order).

    Every metric is None (not 0) when there is nothing eligible to average
    over -- an empty average is UNKNOWN, not zero cost.
    """
    per_task: dict[str, list[UsageRecord]] = {}
    for rec in records:
        if rec.task_id is None:
            continue
        per_task.setdefault(rec.task_id, []).append(rec)

    if not per_task:
        return TokenEfficiency(scope=scope, scope_id=scope_id)

    def _known_total(recs: list[UsageRecord]) -> int | None:
        totals = [r.tokens.total for r in recs if r.tokens.total is not None]
        return sum(totals) if totals else None

    def _avg(values: list[float]) -> float | None:
        return (sum(values) / len(values)) if values else None

    all_task_totals: list[int] = []
    success_totals: list[int] = []
    verified_totals: list[int] = []
    retry_overheads: list[int] = []
    success_count = 0
    verified_count = 0
    retried_count = 0

    for task_id, recs in per_task.items():
        total = _known_total(recs)
        if total is not None:
            all_task_totals.append(total)

        if task_id in success_task_ids:
            success_count += 1
            if total is not None:
                success_totals.append(total)
        if task_id in verified_task_ids:
            verified_count += 1
            if total is not None:
                verified_totals.append(total)

        if len(recs) > 1:
            first_total = recs[0].tokens.total
            if total is not None and first_total is not None:
                retried_count += 1
                retry_overheads.append(total - first_total)

    return TokenEfficiency(
        scope=scope,
        scope_id=scope_id,
        task_count=len(per_task),
        success_task_count=success_count,
        verified_success_task_count=verified_count,
        retried_task_count=retried_count,
        tokens_per_task=_avg(all_task_totals),
        tokens_per_success=_avg(success_totals),
        tokens_per_verified_success=_avg(verified_totals),
        retry_token_overhead=_avg(retry_overheads),
    )
