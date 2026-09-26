"""Evaluation records (Octavryn SI v0.5 spec 01 runtime flow "Evaluation",
spec 04 "Evaluation may record ...", spec 09 gate 9).

After an execution attempt sequence finishes, one EvaluationRecord is
stored per task execution:
- outcome: verified_success (Task reached COMPLETE, i.e. the Definition
  of Done passed), success (a result was recorded but is not verified),
  verification_failed (a result was recorded but a checkable Definition
  of Done criterion failed, task REMEDIATION_REQUIRED; D78),
  failure (no successful result), unknown (nothing ran, e.g. every
  candidate was unavailable);
- retries: attempts that actually executed, minus one;
- tokens: input+output only when both are known; otherwise None
  (UNKNOWN, never 0);
- latency_s: from the final result's timestamps, else None;
- participants: every adapter that was tried, in order (the
  "participant combinations" of spec 04).

Evaluation is evidence for later analysis. It never changes the task's
status and never grants anything.
"""

from __future__ import annotations

from datetime import datetime

from .descriptors import EvaluationRecord
from .state import StateStore


def outcome_for(final_status: str | None, task_status: str | None) -> str:
    if final_status is None:
        return "unknown"
    if final_status == "FAILED":
        return "failure"
    if task_status == "COMPLETE":
        return "verified_success"
    if task_status == "REMEDIATION_REQUIRED":
        return "verification_failed"
    return "success"


def _latency(result) -> float | None:
    try:
        return (datetime.fromisoformat(result.finished_at) - datetime.fromisoformat(result.started_at)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None


def _tokens(result) -> int | None:
    usage = getattr(result, "usage", None)
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    return usage.input_tokens + usage.output_tokens


def record(store: StateStore, task, results: list, skill_id: str | None = None) -> EvaluationRecord:
    """results: the TaskResults that were actually produced, in order
    (skipped/unavailable candidates are not results)."""
    final = results[-1] if results else None
    stored = store.get_task(task.task_id)
    rec = EvaluationRecord(
        task_id=task.task_id,
        intelligence_id=final.agent if final else "none",
        skill_id=skill_id,
        outcome=outcome_for(final.status if final else None, stored["status"] if stored else None),
        retries=max(0, len(results) - 1),
        tokens=_tokens(final) if final else None,
        latency_s=_latency(final) if final else None,
        participants=[r.agent for r in results],
    )
    store.save_evaluation(rec)
    return rec
