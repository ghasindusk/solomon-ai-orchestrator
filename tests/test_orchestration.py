import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth
from solomon.models import Risk, Task
from solomon.orchestration import build_review_task, needs_review
from solomon.router import Router


def make_task(risk=Risk.NORMAL) -> Task:
    return Task(
        goal_id="g1",
        project_id="p1",
        type="test",
        role="coder",
        definition_of_done=["x"],
        risk=risk,
    )


def test_needs_review_true_for_high_and_above():
    assert needs_review(make_task(Risk.HIGH))
    assert needs_review(make_task(Risk.VERY_HIGH))
    assert needs_review(make_task(Risk.CRITICAL))
    assert not needs_review(make_task(Risk.NORMAL))
    assert not needs_review(make_task(Risk.LOW))


def test_build_review_task_picks_different_adapter_than_implementer():
    router = Router()
    original = make_task(Risk.HIGH)
    health_checks = {"claude_code": AdapterHealth(True), "codex": AdapterHealth(True)}
    review_task, score = build_review_task(
        original, router, implementer_agent="claude_code", health_checks=health_checks
    )
    assert review_task is not None
    assert review_task.role == "reviewer"
    assert review_task.assigned_agent != "claude_code"
    assert review_task.dependencies == [original.task_id]
    assert score.adapter_name == review_task.assigned_agent


def test_build_review_task_returns_none_when_no_other_candidate(monkeypatch):
    router = Router()
    monkeypatch.setattr(router, "candidates_for_role", lambda role: ["claude_code"])
    original = make_task(Risk.HIGH)
    review_task, score = build_review_task(
        original,
        router,
        implementer_agent="claude_code",
        health_checks={"claude_code": AdapterHealth(True)},
    )
    assert review_task is None
    assert score is None
