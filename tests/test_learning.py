import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth
from solomon.models import Task
from solomon.result import TaskResult
from solomon.router import Router
from solomon.state import StateStore


def make_task(role="coder", project_id="p1") -> Task:
    return Task(goal_id="g1", project_id=project_id, type="t", role=role, definition_of_done=["x"])


def seed_result(store, project_id, role, agent, status):
    task = Task(goal_id="g1", project_id=project_id, type="t", role=role, definition_of_done=["x"])
    store.save_task(task)
    store.save_result(
        TaskResult(
            task_id=task.task_id, status=status, summary="x", agent=agent,
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
        )
    )


def test_role_fitness_neutral_below_min_samples(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    seed_result(store, "p1", "coder", "claude_code", "RESULT_RECEIVED")
    seed_result(store, "p1", "coder", "claude_code", "RESULT_RECEIVED")  # only 2, need 3
    router = Router(state=store)
    scored = router.score_adapter("claude_code", make_task(role="coder"), health=AdapterHealth(True))
    assert scored.components["role_fitness"] == 0.5
    assert any("role_fitness" in n for n in scored.notes)


def test_role_fitness_reflects_real_success_rate_once_enough_samples(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    for _ in range(2):
        seed_result(store, "p1", "coder", "claude_code", "RESULT_RECEIVED")
    seed_result(store, "p1", "coder", "claude_code", "FAILED")
    router = Router(state=store)
    scored = router.score_adapter("claude_code", make_task(role="coder"), health=AdapterHealth(True))
    assert abs(scored.components["role_fitness"] - (2 / 3)) < 1e-6


def test_role_fitness_is_role_specific_not_global(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    # Great at "coder", terrible at "documenter" -- role_fitness must not blend the two.
    for _ in range(4):
        seed_result(store, "p1", "coder", "claude_code", "RESULT_RECEIVED")
    for _ in range(4):
        seed_result(store, "p1", "documenter", "claude_code", "FAILED")
    router = Router(state=store)

    coder_score = router.score_adapter("claude_code", make_task(role="coder"), health=AdapterHealth(True))
    doc_score = router.score_adapter("claude_code", make_task(role="documenter"), health=AdapterHealth(True))
    assert coder_score.components["role_fitness"] == 1.0
    assert doc_score.components["role_fitness"] == 0.0


def test_get_role_performance_table_shape(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    seed_result(store, "p1", "coder", "claude_code", "RESULT_RECEIVED")
    seed_result(store, "p1", "coder", "claude_code", "FAILED")
    seed_result(store, "p1", "reviewer", "codex", "RESULT_RECEIVED")

    table = store.get_role_performance()
    assert table["coder"]["claude_code"]["count"] == 2
    assert table["coder"]["claude_code"]["successes"] == 1
    assert table["coder"]["claude_code"]["success_rate"] == 0.5
    assert table["reviewer"]["codex"]["success_rate"] == 1.0


def test_get_role_performance_scoped_by_project(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    seed_result(store, "proj_a", "coder", "claude_code", "RESULT_RECEIVED")
    seed_result(store, "proj_b", "coder", "claude_code", "FAILED")

    table_a = store.get_role_performance(project_id="proj_a")
    assert table_a["coder"]["claude_code"]["count"] == 1
    assert table_a["coder"]["claude_code"]["success_rate"] == 1.0
