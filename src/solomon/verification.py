"""Definition of Done verification (FR-09, architecture doc section 3
"Completion Semantics" / GLOBAL_POLICY.yaml completion block).

Closes a gap noted since Phase 1: every Task previously stalled at
RESULT_RECEIVED forever because nothing ever checked its declared
`definition_of_done` and promoted it further. This module is that
check -- and only that check. It verifies a Task's own declared DoD
items against a small registry of criteria Solomon can honestly check
(did a result actually get recorded, does a required review Task
exist and itself have a result, does the project's registered build
command actually succeed). Any criterion outside that registry
is reported unverifiable, never silently assumed satisfied -- policy's
`completion.require_applicable_definition_of_done` decides whether an
unverifiable criterion blocks COMPLETE (spec section 6: "Agent output
is evidence, not completion. Solomon owns state transition").
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .models import Task, TaskStatus
from .policy import PolicyEngine
from .registry import ProjectRegistry
from .state import StateStore

_BUILD_TIMEOUT_S = 600


@dataclass
class DoDVerification:
    satisfied: list[str] = field(default_factory=list)
    unsatisfied: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)


def _check_result_recorded(task: Task, store: StateStore) -> bool:
    results = store.get_task_results(task.task_id)
    return any(r.get("status") == "RESULT_RECEIVED" for r in results)


def _check_review_of(criterion: str, task: Task, store: StateStore) -> bool:
    target_task_id = criterion.split(":", 1)[1]
    for other in store.list_tasks(project_id=task.project_id):
        if other.get("role") != "reviewer":
            continue
        if target_task_id not in (other.get("dependencies") or []):
            continue
        if other.get("status") in ("COMPLETE", "RESULT_RECEIVED"):
            return True
    return False


def _check_build_passes(task: Task, project_registry: ProjectRegistry) -> bool | None:
    r"""None (unverifiable) when the project has no registered
    build_command/repo_path -- that's a config gap, not a failed build.
    Runs the real command (a config value from our own registry YAML,
    not user/model-controlled text) via shell=True since Windows build
    scripts (gradlew.bat, flutter.bat) are batch files needing it, same
    as codex_adapter.py's precedent.

    Confirmed real gotcha on this machine: cmd.exe (via Python's
    shell=True) does NOT implicitly search the working directory for a
    bare relative script name -- `gradlew.bat compileJava` fails with
    "not recognized as an internal or external command" even with cwd
    set correctly, while `.\gradlew.bat compileJava` succeeds. This is
    a build_command *authoring* concern (registry.yaml must prefix
    repo-relative Windows scripts with `.\`), not something this
    function silently rewrites -- PATH-resolved commands like `flutter
    analyze` are unaffected and must NOT be prefixed."""
    project = project_registry.get(task.project_id)
    if project is None or not project.build_command or not project.repo_path:
        return None
    try:
        proc = subprocess.run(
            project.build_command,
            shell=True,
            cwd=project.repo_path,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BUILD_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def verify_definition_of_done(
    task: Task, store: StateStore, project_registry: ProjectRegistry | None = None
) -> DoDVerification:
    verification = DoDVerification()
    for criterion in task.definition_of_done:
        if criterion == "result_recorded":
            ok = _check_result_recorded(task, store)
        elif criterion.startswith("review_of:"):
            ok = _check_review_of(criterion, task, store)
        elif criterion == "build_passes":
            registry = project_registry or ProjectRegistry()
            ok = _check_build_passes(task, registry)
            if ok is None:
                verification.unverifiable.append(criterion)
                continue
        else:
            verification.unverifiable.append(criterion)
            continue
        (verification.satisfied if ok else verification.unsatisfied).append(criterion)
    return verification


_VERIFIABLE_STATUSES = (TaskStatus.RESULT_RECEIVED, TaskStatus.REMEDIATION_REQUIRED)


def advance_after_result(
    task: Task, store: StateStore, policy: PolicyEngine, project_registry: ProjectRegistry | None = None
) -> TaskStatus:
    """Call once a Task has reached RESULT_RECEIVED, and again any time
    later to re-check one still at REMEDIATION_REQUIRED (e.g. `verify-task`
    after a blocking review finally lands) -- never for FAILED (no result
    to verify) or an already-settled COMPLETE. Returns the settled status:
    COMPLETE, REMEDIATION_REQUIRED (a declared criterion is checkably
    unsatisfied), or RESULT_RECEIVED unchanged (an unverifiable criterion
    exists and policy requires DoD, so this needs a human/manual call,
    not an automatic promotion)."""
    if task.status not in _VERIFIABLE_STATUSES:
        return task.status

    verification = verify_definition_of_done(task, store, project_registry)
    if verification.unsatisfied:
        return TaskStatus.REMEDIATION_REQUIRED
    if verification.unverifiable and policy.definition_of_done_required():
        return TaskStatus.RESULT_RECEIVED
    return TaskStatus.COMPLETE
