"""Parallel-safe execution -- completes Phase 3 (Architecture doc section 7
"Concurrency" / FR-15).

Everything before this module (router, review tasks, the file-lock table)
ran one task at a time; this is the actual concurrent executor the
roadmap's "Parallel safe execution" entry was waiting on. A task uses
one of two isolation strategies for concurrent writes, never both at
once conceptually (though nothing stops declaring both):

- `touches`: advisory path lock (StateStore.acquire_locks) -- on
  conflict the task is deferred for this batch rather than risked;
  callers can resubmit deferred items in a later batch once the holder
  is done, this module does not auto-retry them.
- `use_worktree`: a real, isolated `git worktree` + branch
  (worktree.WorktreeManager) -- the adapter executes with that
  directory as its cwd, so concurrent worktree-isolated tasks can
  genuinely write to the same paths without ever conflicting, each in
  its own branch. Merge is a deliberately separate, later step (see
  cli.py `worktree merge`), never automatic here -- "merge occurs only
  after verification" (architecture doc section 7).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .models import Task, task_status_from_result_status
from .policy import PolicyEngine
from .result import TaskResult
from .router import Router
from .state import StateStore
from .verification import advance_after_result
from .worktree import WorktreeManager


@dataclass
class BatchItem:
    task: Task
    prompt: str
    touches: list[str] = field(default_factory=list)
    timeout_s: int = 600
    use_worktree: bool = False
    base_branch: str = "main"


@dataclass
class BatchOutcome:
    task_id: str
    status: str  # "completed" | "deferred_lock_conflict" | "no_candidate" | "worktree_failed"
    #              | "waiting_approval" | "denied" (v0.5 R6 governance)
    result: TaskResult | None = None
    detail: str = ""
    worktree_path: str | None = None


def _run_one(
    item: BatchItem,
    router: Router,
    adapter_factory,
    state: StateStore,
    policy: PolicyEngine,
    worktree_manager: WorktreeManager | None = None,
    worktrees_root: Path | None = None,
    gpu_telemetry: dict | None = None,
) -> BatchOutcome:
    # v0.5 R6: the same gates as run-task/route-and-run, before any lock,
    # worktree or adapter is touched.
    from .governance import evaluate, request_approval

    decision = evaluate(item.task, item.prompt, state, policy)
    if decision.denied:
        return BatchOutcome(task_id=item.task.task_id, status="denied", detail=decision.reason)
    if decision.requires_approval:
        if item.use_worktree:
            return BatchOutcome(
                task_id=item.task.task_id, status="waiting_approval",
                detail=f"{decision.reason} -- worktree items are not queued for approval; "
                       "rerun this item alone with route-and-run to get an approval request",
            )
        request_id = request_approval(state, item.task, decision, {
            "kind": "route", "prompt": item.prompt, "timeout": item.timeout_s, "max_attempts": 1,
        })
        return BatchOutcome(
            task_id=item.task.task_id, status="waiting_approval",
            detail=f"approval request {request_id}: {decision.reason}",
        )
    if item.touches:
        acquired = state.acquire_locks(item.task.task_id, item.task.project_id, item.touches)
        if not acquired:
            return BatchOutcome(
                task_id=item.task.task_id,
                status="deferred_lock_conflict",
                detail=f"paths already locked by another running task: {item.touches}",
            )

    worktree_path: Path | None = None
    try:
        if item.use_worktree:
            if worktree_manager is None or worktrees_root is None:
                return BatchOutcome(
                    task_id=item.task.task_id,
                    status="worktree_failed",
                    detail="use_worktree requested but no WorktreeManager/worktrees_root configured",
                )
            worktree_path, wt_result = worktree_manager.create(
                item.task.task_id, worktrees_root, item.base_branch
            )
            if worktree_path is None:
                return BatchOutcome(task_id=item.task.task_id, status="worktree_failed", detail=wt_result.detail)
            state.create_worktree_record(
                item.task.task_id,
                item.task.project_id,
                str(worktree_path),
                f"solomon/{item.task.task_id}",
                item.base_branch,
            )

        candidates = router.candidates_for_role(item.task.role)
        if not candidates:
            return BatchOutcome(
                task_id=item.task.task_id,
                status="no_candidate",
                detail=f"no adapter maps to role '{item.task.role}'",
            )
        health_checks = {name: _make(adapter_factory, name, item).health() for name in candidates}
        scores = router.route(item.task, health_checks=health_checks, gpu_telemetry=gpu_telemetry)
        available = [s for s in scores if health_checks[s.adapter_name].available]
        if not available:
            return BatchOutcome(
                task_id=item.task.task_id, status="no_candidate", detail="no available adapter for role"
            )

        chosen_name = available[0].adapter_name
        adapter = _make(adapter_factory, chosen_name, item, cwd=str(worktree_path) if worktree_path is not None else None)
        result = adapter.execute(item.task, item.prompt, timeout_s=item.timeout_s)
        state.save_result(result)
        item.task.status = task_status_from_result_status(result.status)
        item.task.status = advance_after_result(item.task, state, policy)
        state.save_task(item.task)
        from .evaluation import record as record_evaluation

        record_evaluation(state, item.task, [result])
        return BatchOutcome(
            task_id=item.task.task_id,
            status="completed",
            result=result,
            worktree_path=str(worktree_path) if worktree_path else None,
        )
    finally:
        if item.touches:
            state.release_locks(item.task.task_id)


def _make(adapter_factory, name: str, item, cwd: str | None = None):
    """D66: a factory marked `project_aware` gets the item's project_id,
    so each item runs in its own project's repo_path under that project's
    policy. Plain factories keep the v0.4 call shapes."""
    if getattr(adapter_factory, "project_aware", False):
        return adapter_factory(name, cwd=cwd, project_id=item.task.project_id)
    return adapter_factory(name, cwd=cwd) if cwd is not None else adapter_factory(name)


def run_batch(
    items: list[BatchItem],
    router: Router,
    adapter_factory,
    state: StateStore,
    max_workers: int = 4,
    worktree_manager: WorktreeManager | None = None,
    worktrees_root: Path | str | None = None,
    gpu_telemetry: dict | None = None,
) -> list[BatchOutcome]:
    """gpu_telemetry (telemetry.get_gpu_telemetry()), like
    worktree_manager, is gathered by the caller and passed in rather
    than fetched here -- keeps this function testable without touching
    real hardware, same reasoning as dashboard.py's build_dashboard."""
    policy = PolicyEngine()  # loaded once, shared read-only across worker threads
    worktrees_root = Path(worktrees_root) if worktrees_root else None
    for item in items:
        state.save_task(item.task)

    outcomes: list[BatchOutcome] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _run_one, item, router, adapter_factory, state, policy,
                worktree_manager, worktrees_root, gpu_telemetry,
            ): item.task.task_id
            for item in items
        }
        for future in as_completed(futures):
            outcomes.append(future.result())
    return outcomes
