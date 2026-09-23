"""Tests for Replay & Router Comparison (Phase 7, DECISIONS.md D31)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.adapters.base import AdapterHealth
from solomon.models import Task
from solomon.replay import compare_routers, replay_task
from solomon.result import TaskResult, Usage
from solomon.router import Router
from solomon.state import StateStore


def make_store(tmp_path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.sqlite3")


def seed(store, project_id="proj-1", agent="codex") -> Task:
    task = Task(goal_id="g1", project_id=project_id, type="t", role="coder", definition_of_done=["x"])
    store.save_task(task)
    store.save_result(
        TaskResult(
            task_id=task.task_id, status="RESULT_RECEIVED", summary="ok", agent=agent,
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
    )
    return task


def test_replay_unknown_task_id_raises(tmp_path):
    store = make_store(tmp_path)
    router = Router()
    with pytest.raises(ValueError):
        replay_task("no-such-task", store, router)


def test_replay_never_writes_to_state(tmp_path):
    store = make_store(tmp_path)
    task = seed(store)
    router = Router(state=store)
    before_task = store.get_task(task.task_id)
    before_results = store.get_task_results(task.task_id)

    replay_task(task.task_id, store, router, health_checks={"claude_code": AdapterHealth(True)})

    assert store.get_task(task.task_id) == before_task
    assert store.get_task_results(task.task_id) == before_results


def test_replay_reports_original_agent_from_task_results(tmp_path):
    store = make_store(tmp_path)
    task = seed(store, agent="codex")
    router = Router(state=store)
    result = replay_task(task.task_id, store, router)
    assert result.original_agent == "codex"


def test_replay_original_agent_none_when_no_results_recorded(tmp_path):
    store = make_store(tmp_path)
    task = Task(goal_id="g1", project_id="proj-1", type="t", role="coder", definition_of_done=["x"])
    store.save_task(task)
    router = Router(state=store)
    result = replay_task(task.task_id, store, router)
    assert result.original_agent is None
    assert result.decision_changed is None  # UNKNOWN, not False


def _seed_strong_history(store, agent, role="coder", project_id="proj-1", n=3):
    for _ in range(n):
        t = Task(goal_id="g1", project_id=project_id, type="t", role=role, definition_of_done=["x"])
        store.save_task(t)
        store.save_result(
            TaskResult(
                task_id=t.task_id, status="RESULT_RECEIVED", summary="ok", agent=agent,
                started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
                usage=Usage(input_tokens=10, output_tokens=5),
            )
        )


def test_decision_changed_true_when_replayed_choice_differs(tmp_path):
    store = make_store(tmp_path)
    task = seed(store, agent="claude_code")  # weak: one result, no track record built up
    _seed_strong_history(store, "codex")  # codex now clearly outscores on historical_quality/role_fitness
    router = Router(state=store)
    result = replay_task(
        task.task_id, store, router,
        health_checks={"claude_code": AdapterHealth(True), "codex": AdapterHealth(True)},
    )
    assert result.replayed_top_choice == "codex"
    assert result.decision_changed is True


def test_decision_changed_false_when_same(tmp_path):
    store = make_store(tmp_path)
    task = seed(store, agent="claude_code")
    _seed_strong_history(store, "claude_code")
    router = Router(state=store)
    result = replay_task(
        task.task_id, store, router,
        health_checks={"claude_code": AdapterHealth(True), "codex": AdapterHealth(True)},
    )
    assert result.replayed_top_choice == "claude_code"
    assert result.decision_changed is False


def test_compare_routers_empty_list_gives_empty_report(tmp_path):
    store = make_store(tmp_path)
    router = Router(state=store)
    report = compare_routers([], store, router, router)
    assert report.task_count == 0
    assert report.change_rate is None


def test_compare_routers_skips_nonexistent_task_ids(tmp_path):
    store = make_store(tmp_path)
    task = seed(store)
    router = Router(state=store)
    report = compare_routers([task.task_id, "ghost-task"], store, router, router)
    assert report.task_count == 1  # ghost-task silently skipped


def test_compare_routers_identical_routers_never_change(tmp_path):
    store = make_store(tmp_path)
    task = seed(store)
    router = Router(state=store)
    report = compare_routers([task.task_id], store, router, router)
    assert report.changed_count == 0
    assert report.change_rate == 0.0


def test_compare_routers_detects_changed_decision_via_weights(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    task = seed(store)

    import yaml
    weights_path = tmp_path / "weights.yaml"
    weights_path.write_text(
        yaml.safe_dump({"weights": {"skill_match": 1.0}, "adapters": {}}),
        encoding="utf-8",
    )
    router_a = Router(state=store)
    router_b = Router(state=store, weights_path=weights_path)

    report = compare_routers([task.task_id], store, router_a, router_b)
    assert report.task_count == 1
    assert report.changed_count == 1
    assert report.change_rate == 1.0
    assert report.changes[0]["task_id"] == task.task_id
    assert report.changes[0]["router_a"] != report.changes[0]["router_b"]
