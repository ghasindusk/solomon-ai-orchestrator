"""Retry/fallback execution -- lean Phase 5 slice (Architecture doc
section 10 "Failure Handling").

"Retry only for retryable failures... Provider outage triggers eligible
fallback routing." This only retries/falls back on execution-layer
failures (adapter unavailable, timeout, subprocess/transport error) --
never on a semantic failure (the agent ran fine and returned a result
Solomon or a reviewer just doesn't like). Semantic failure is FR-08's
job (review/remediation Tasks via orchestration.py), not this module's:
blindly re-running the same prompt against another adapter because the
first answer looked wrong would just be guessing, not fallback routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .adapters.base import AdapterHealth, AgentAdapter
from .models import Task, task_status_from_result_status
from .policy import PolicyEngine
from .result import TaskResult
from .router import AgentScore, Router
from .state import StateStore
from .verification import advance_after_result

_RETRYABLE_MARKERS = ("timeout", "subprocess_error", "unavailable", "not found on path")


def _is_retryable_failure(result: TaskResult, health: AdapterHealth | None) -> bool:
    if health is not None and not health.available:
        return True
    if result.status != "FAILED":
        return False
    joined = " ".join(result.uncertainties).lower()
    return any(marker in joined for marker in _RETRYABLE_MARKERS)


@dataclass
class FallbackAttempt:
    adapter_name: str
    result: TaskResult | None
    health: AdapterHealth
    skipped: bool = False


@dataclass
class ExecutionOutcome:
    final_result: TaskResult | None
    attempts: list[FallbackAttempt] = field(default_factory=list)
    # v0.5 R6: set when governance stopped execution before any adapter ran.
    blocked: object | None = None  # governance.GovernanceDecision


def execute_with_fallback(
    task: Task,
    prompt: str,
    router: Router,
    adapter_factory,
    state: StateStore | None = None,
    policy: PolicyEngine | None = None,
    max_attempts: int = 2,
    timeout_s: int = 600,
    gpu_telemetry: dict | None = None,
    authorized: bool = False,
    skill=None,
) -> ExecutionOutcome:
    """adapter_factory(name) -> AgentAdapter, so this module doesn't import
    the CLI's adapter-lookup table (keeps it independent/testable). policy
    defaults to loading GLOBAL_POLICY.yaml when state is given and policy
    is omitted -- pass one explicitly to avoid the redundant file read
    across many calls, or to use a non-default policy in tests.
    gpu_telemetry (telemetry.get_gpu_telemetry()) is forwarded to the
    router so a local/GPU-bound adapter is penalized while the GPU is
    busy with something else (e.g. the user playing Minecraft)."""
    if state is not None and policy is None:
        policy = PolicyEngine()
    # v0.5 R6 defense in depth: a caller that has not already run the
    # governance gates (or consumed an approval) gets them here, so library
    # use cannot bypass what the CLI enforces. Without a StateStore there
    # is nowhere to record an approval, so it fails closed on anything that
    # is not plainly allowed.
    if not authorized:
        from .governance import evaluate

        decision = evaluate(task, prompt, state, policy, skill=skill) if state is not None else None
        if decision is None:
            from .governance import GateResult, GovernanceDecision

            decision = GovernanceDecision(risk=task.risk, gates=[GateResult(
                "state", "deny", "no StateStore: cannot evaluate or record governance")])
        if not decision.allowed:
            return ExecutionOutcome(final_result=None, blocked=decision)
    # skill is forwarded only when given, keeping the v0.4 call shapes of
    # candidates_for_role/route (both are override seams).
    skill_kw = {"skill": skill} if skill is not None else {}
    candidates = router.candidates_for_role(task.role, **skill_kw)
    health_checks = {name: adapter_factory(name).health() for name in candidates}
    scores: list[AgentScore] = router.route(task, health_checks=health_checks, gpu_telemetry=gpu_telemetry, **skill_kw)

    outcome = ExecutionOutcome(final_result=None)
    for score in scores[:max_attempts]:
        adapter: AgentAdapter = adapter_factory(score.adapter_name)
        health = health_checks[score.adapter_name]
        if not health.available:
            outcome.attempts.append(
                FallbackAttempt(adapter_name=score.adapter_name, result=None, health=health, skipped=True)
            )
            if state is not None:
                state.log_event(
                    project_id=task.project_id,
                    task_id=task.task_id,
                    agent=score.adapter_name,
                    event="fallback_skip_unavailable",
                )
            continue

        result = adapter.execute(task, prompt, timeout_s=timeout_s)
        outcome.attempts.append(
            FallbackAttempt(adapter_name=score.adapter_name, result=result, health=health)
        )
        if state is not None:
            state.save_result(result)
            task.status = task_status_from_result_status(result.status)
            task.status = advance_after_result(task, state, policy)
            state.save_task(task)

        if result.status != "FAILED" or not _is_retryable_failure(result, health):
            outcome.final_result = result
            _evaluate(state, task, outcome, skill)
            return outcome

        if state is not None:
            state.log_event(
                project_id=task.project_id,
                task_id=task.task_id,
                agent=score.adapter_name,
                event="fallback_retry",
                detail=result.status,
            )

    # D77: the final result is the last adapter that actually ran. A later
    # *skipped* candidate must not erase it (a real FAILED would otherwise be
    # reported as "no adapter produced a result" and relabelled BLOCKED).
    executed = [a.result for a in outcome.attempts if a.result is not None]
    if executed:
        outcome.final_result = executed[-1]
    if outcome.final_result is None and state is not None:
        # D71: every candidate was unavailable (or none existed). Before,
        # the Task stayed QUEUED forever; BLOCKED says "nothing could run
        # it" and keeps it retryable once a provider is back.
        from .models import TaskStatus

        task.status = TaskStatus.BLOCKED
        state.save_task(task)
        state.log_event(project_id=task.project_id, task_id=task.task_id, event="no_available_adapter",
                        detail=",".join(a.adapter_name for a in outcome.attempts) or "no candidates")
    _evaluate(state, task, outcome, skill)
    return outcome


def _evaluate(state, task, outcome, skill) -> None:
    """v0.5 spec 09 gate 9: one EvaluationRecord per execution sequence."""
    if state is None:
        return
    from .evaluation import record

    results = [a.result for a in outcome.attempts if a.result is not None]
    record(state, task, results, skill_id=skill.id if skill is not None else None)
