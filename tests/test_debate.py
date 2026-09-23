import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.debate import run_debate
from solomon.models import Task
from solomon.policy import PolicyEngine
from solomon.result import TaskResult


class FakeAdapter(AgentAdapter):
    def __init__(self, name, answer):
        self.name = name
        self.answer = answer
        self.execute_calls = 0

    def health(self) -> AdapterHealth:
        return AdapterHealth(True)

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        self.execute_calls += 1
        return TaskResult(
            task_id=task.task_id,
            status="RESULT_RECEIVED",
            summary=self.answer,
            agent=self.name,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:01+00:00",
        )


class EnabledPolicy(PolicyEngine):
    def __init__(self, max_agents=3, max_rounds=2):
        self._policy = {
            "communication": {
                "debate": {"enabled": True, "default": False, "max_agents": max_agents, "max_rounds": max_rounds}
            }
        }


class DisabledPolicy(PolicyEngine):
    def __init__(self):
        self._policy = {"communication": {"debate": {"enabled": False}}}


def make_task() -> Task:
    return Task(goal_id="g1", project_id="p1", type="debate", role="reviewer", definition_of_done=["x"])


def test_disabled_policy_aborts():
    participants = {"claude_code": FakeAdapter("claude_code", "A"), "codex": FakeAdapter("codex", "B")}
    judge = FakeAdapter("claude_code", "final")
    outcome = run_debate(make_task(), "q", participants, "claude_code", judge, DisabledPolicy())
    assert outcome.aborted_reason is not None
    assert outcome.final_result is None


def test_too_few_participants_aborts():
    participants = {"claude_code": FakeAdapter("claude_code", "A")}
    judge = FakeAdapter("claude_code", "final")
    outcome = run_debate(make_task(), "q", participants, "claude_code", judge, EnabledPolicy())
    assert "at least 2" in outcome.aborted_reason


def test_exceeds_max_agents_aborts():
    participants = {
        "a": FakeAdapter("a", "1"), "b": FakeAdapter("b", "2"),
        "c": FakeAdapter("c", "3"), "d": FakeAdapter("d", "4"),
    }
    judge = FakeAdapter("a", "final")
    outcome = run_debate(make_task(), "q", participants, "a", judge, EnabledPolicy(max_agents=3))
    assert "max_agents" in outcome.aborted_reason


def test_runs_configured_rounds_and_calls_judge():
    claude = FakeAdapter("claude_code", "answer A")
    codex = FakeAdapter("codex", "answer B")
    judge = FakeAdapter("claude_code", "synthesized final answer")
    outcome = run_debate(
        make_task(), "q", {"claude_code": claude, "codex": codex}, "claude_code", judge,
        EnabledPolicy(max_rounds=2),
    )
    assert outcome.aborted_reason is None
    assert len(outcome.rounds) == 2
    assert claude.execute_calls == 2
    assert codex.execute_calls == 2
    assert outcome.final_result.summary == "synthesized final answer"
    assert judge.execute_calls == 1


def test_second_round_prompt_includes_first_round_answers():
    seen_prompts = []

    class RecordingAdapter(AgentAdapter):
        def __init__(self, name):
            self.name = name

        def health(self):
            return AdapterHealth(True)

        def execute(self, task, prompt, timeout_s=600):
            seen_prompts.append(prompt)
            return TaskResult(
                task_id=task.task_id, status="RESULT_RECEIVED", summary=f"{self.name}-answer",
                agent=self.name, started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:01+00:00",
            )

    a, b = RecordingAdapter("a"), RecordingAdapter("b")
    judge = FakeAdapter("a", "final")
    run_debate(make_task(), "original question", {"a": a, "b": b}, "a", judge, EnabledPolicy(max_rounds=2))
    round2_prompts = seen_prompts[2:]
    assert all("a-answer" in p or "b-answer" in p for p in round2_prompts)
