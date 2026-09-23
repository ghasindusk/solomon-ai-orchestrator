"""Review/remediation task creation (FR-08).

Solomon may create review Tasks after an implementer TaskResult; for
high-risk work, implementer and reviewer should be separated when
resources permit (spec section 2 FR-08). This is a lean helper, not a
full Director -- it builds and routes the follow-up Task, it does not
decide when to call it (callers decide, e.g. after a HIGH+ risk task
completes) or execute it.
"""

from __future__ import annotations

from .adapters.base import AdapterHealth
from .models import CommunicationMode, Risk, Task
from .router import AgentScore, Router


def needs_review(task: Task) -> bool:
    return task.risk in (Risk.HIGH, Risk.VERY_HIGH, Risk.CRITICAL)


def build_review_task(
    original: Task,
    router: Router,
    implementer_agent: str,
    health_checks: dict[str, AdapterHealth] | None = None,
) -> tuple[Task, AgentScore] | tuple[None, None]:
    """Build a review Task for `original` and route it to the
    highest-scoring reviewer candidate that is NOT `implementer_agent`.
    Returns (None, None) if no other reviewer candidate exists -- the
    caller must then decide (per policy) whether to proceed without
    implementer/reviewer separation rather than silently reusing the
    same adapter."""
    review_task = Task(
        goal_id=original.goal_id,
        project_id=original.project_id,
        type="review",
        role="reviewer",
        # The review Task's own completion evidence is just its own
        # result -- "review_of:<id>" is what the ORIGINAL task's DoD
        # declares to require this review; putting the same criterion
        # here would make the review task's DoD check look for a
        # reviewer task depending on `original` and find itself, before
        # its own status update had even been saved -- a self-referential
        # bug that left every review task stuck at REMEDIATION_REQUIRED.
        definition_of_done=["result_recorded"],
        risk=original.risk,
        dependencies=[original.task_id],
        communication_mode=CommunicationMode.REVIEW,
    )

    scores = router.route(review_task, health_checks=health_checks)
    other_scores = [s for s in scores if s.adapter_name != implementer_agent]
    if not other_scores:
        return None, None

    chosen = other_scores[0]
    review_task.assigned_agent = chosen.adapter_name
    return review_task, chosen
