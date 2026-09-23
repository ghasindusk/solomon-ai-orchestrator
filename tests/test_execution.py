import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.execution import execute_with_fallback
from solomon.models import Task
from solomon.result import TaskResult
from solomon.router import AgentScore, Router
from solomon.state import StateStore


class FakeAdapter(AgentAdapter):
    def __init__(self, name, available=True, result_status="RESULT_RECEIVED", uncertainties=None):
        self.name = name
        self._available = available
        self._result_status = result_status
        self._uncertainties = uncertainties or []
        self.execute_calls = 0

    def health(self) -> AdapterHealth:
        return AdapterHealth(self._available, "" if self._available else "down")

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        self.execute_calls += 1
        return TaskResult(
            task_id=task.task_id,
            status=self._result_status,
            summary="ok" if self._result_status == "RESULT_RECEIVED" else "failed",
            agent=self.name,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
            uncertainties=self._uncertainties,
        )


def make_task() -> Task:
    return Task(goal_id="g1", project_id="p1", type="t", role="coder", definition_of_done=["x"])


def _restrict_candidates(router, monkeypatch, names):
    # Real candidates_for_role reflects agents.example.yaml's capability
    # tags, which may include more than the two adapters these tests fake
    # out (e.g. antigravity also claims "coding"); pin it down so tests
    # don't depend on that config's exact contents.
    monkeypatch.setattr(router, "candidates_for_role", lambda role: list(names))


def test_succeeds_on_first_candidate(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict_candidates(router, monkeypatch, ["claude_code", "codex"])
    adapters = {"claude_code": FakeAdapter("claude_code"), "codex": FakeAdapter("codex")}
    outcome = execute_with_fallback(make_task(), "hi", router, lambda name: adapters[name], state=store)
    assert outcome.final_result.status == "RESULT_RECEIVED"
    assert len(outcome.attempts) == 1


def test_falls_back_when_first_unavailable(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict_candidates(router, monkeypatch, ["claude_code", "codex"])
    # Force a specific ranking (claude_code first, despite being
    # unavailable) so we exercise the skip-and-continue path deterministically,
    # independent of the real scoring formula's own ranking behavior.
    monkeypatch.setattr(
        router,
        "route",
        lambda task, health_checks=None, gpu_telemetry=None: [
            AgentScore(adapter_name="claude_code", total=0.9),
            AgentScore(adapter_name="codex", total=0.8),
        ],
    )
    adapters = {
        "claude_code": FakeAdapter("claude_code", available=False),
        "codex": FakeAdapter("codex", available=True),
    }
    outcome = execute_with_fallback(make_task(), "hi", router, lambda name: adapters[name], state=store)
    assert outcome.final_result.status == "RESULT_RECEIVED"
    assert outcome.final_result.agent == "codex"
    skipped = [a for a in outcome.attempts if a.skipped]
    assert len(skipped) == 1
    assert skipped[0].adapter_name == "claude_code"


def test_retries_on_retryable_failure_then_succeeds(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict_candidates(router, monkeypatch, ["claude_code", "codex"])
    adapters = {
        "claude_code": FakeAdapter("claude_code", result_status="FAILED", uncertainties=["timeout"]),
        "codex": FakeAdapter("codex", result_status="RESULT_RECEIVED"),
    }
    outcome = execute_with_fallback(make_task(), "hi", router, lambda name: adapters[name], state=store)
    assert outcome.final_result.status == "RESULT_RECEIVED"
    assert len(outcome.attempts) == 2


def test_does_not_retry_non_retryable_failure(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict_candidates(router, monkeypatch, ["claude_code", "codex"])
    adapters = {
        "claude_code": FakeAdapter("claude_code", result_status="FAILED", uncertainties=["the model refused"]),
        "codex": FakeAdapter("codex", result_status="RESULT_RECEIVED"),
    }
    outcome = execute_with_fallback(make_task(), "hi", router, lambda name: adapters[name], state=store)
    assert outcome.final_result.status == "FAILED"
    assert len(outcome.attempts) == 1
    assert adapters["codex"].execute_calls == 0


def test_task_status_updated_in_state_after_success(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict_candidates(router, monkeypatch, ["claude_code"])
    adapters = {"claude_code": FakeAdapter("claude_code")}
    task = make_task()
    execute_with_fallback(task, "hi", router, lambda name: adapters[name], state=store)
    saved = store.list_tasks(project_id=task.project_id)
    assert saved[0]["status"] == "RESULT_RECEIVED"


def test_respects_max_attempts(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict_candidates(router, monkeypatch, ["claude_code", "codex"])
    adapters = {
        "claude_code": FakeAdapter("claude_code", result_status="FAILED", uncertainties=["timeout"]),
        "codex": FakeAdapter("codex", result_status="FAILED", uncertainties=["timeout"]),
    }
    outcome = execute_with_fallback(
        make_task(), "hi", router, lambda name: adapters[name], state=store, max_attempts=1
    )
    assert len(outcome.attempts) == 1
    assert outcome.final_result.status == "FAILED"
