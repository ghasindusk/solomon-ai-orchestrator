import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.gateway import (
    DelegationDecision,
    GatewayMode,
    InvocationEnvelope,
    build_return_contract,
    should_delegate,
)
from solomon.models import Goal, Risk, Task, TaskStatus
from solomon.state import StateStore


def make_store(tmp_path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.sqlite3")


def test_envelope_round_trip_preserves_all_fields():
    original = InvocationEnvelope(
        request="do the thing", caller_type="claude_code",
        caller_session_id="sess-1", project_hint="proj_a",
        working_directory="/tmp/proj_a", autonomy_override=2,
        privacy_constraints=["no_network"], expected_output="a diff",
    )
    restored = InvocationEnvelope.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()


def test_resolve_project_id_prefers_hint_over_working_directory(tmp_path):
    from solomon.registry import ProjectEntry, ProjectRegistry

    registry = ProjectRegistry.__new__(ProjectRegistry)
    registry._projects = {
        "proj_a": ProjectEntry(project_id="proj_a", name="A", repo_path=str(tmp_path / "a")),
        "proj_b": ProjectEntry(project_id="proj_b", name="B", repo_path=str(tmp_path / "b")),
    }
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    env = InvocationEnvelope(request="x", caller_type="claude_code", project_hint="proj_a",
                              working_directory=str(tmp_path / "b"))
    assert env.resolve_project_id(registry) == "proj_a"


def test_resolve_project_id_falls_back_to_working_directory(tmp_path):
    from solomon.registry import ProjectEntry, ProjectRegistry

    registry = ProjectRegistry.__new__(ProjectRegistry)
    registry._projects = {"proj_a": ProjectEntry(project_id="proj_a", name="A", repo_path=str(tmp_path / "a"))}
    (tmp_path / "a").mkdir()

    env = InvocationEnvelope(request="x", caller_type="claude_code", working_directory=str(tmp_path / "a"))
    assert env.resolve_project_id(registry) == "proj_a"


def test_resolve_project_id_none_when_unregistered(tmp_path):
    from solomon.registry import ProjectRegistry

    registry = ProjectRegistry.__new__(ProjectRegistry)
    registry._projects = {}
    env = InvocationEnvelope(request="x", caller_type="claude_code", working_directory=str(tmp_path))
    assert env.resolve_project_id(registry) is None


def test_should_delegate_force_modes_ignore_content():
    env = InvocationEnvelope(request="explain this", caller_type="claude_code")
    assert should_delegate(env, GatewayMode.FORCE_SOLOMON, project_id="p").delegate is True
    assert should_delegate(env, GatewayMode.FORCE_LOCAL, project_id="p").delegate is False


def test_should_delegate_none_without_registered_project():
    env = InvocationEnvelope(request="implement and test this across files", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.AUTO, project_id=None)
    assert decision.delegate is False
    assert "FR-02" in decision.reason


def test_should_delegate_matches_delegate_signal():
    env = InvocationEnvelope(request="please refactor across files", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.AUTO, project_id="p")
    assert decision.delegate is True
    assert "multi-file / cross-file change" in decision.matched_signals


def test_should_delegate_local_signal_wins_over_delegate_signal():
    # "refactor" would match delegate, but explicit "what is" (local) wins.
    env = InvocationEnvelope(request="what is refactor across files", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.AUTO, project_id="p")
    assert decision.delegate is False


def test_should_delegate_japanese_keywords():
    env = InvocationEnvelope(request="実装してからレビューもお願いします", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.AUTO, project_id="p")
    assert decision.delegate is True


def test_should_delegate_shadow_mode_never_delegates_but_reports():
    env = InvocationEnvelope(request="please refactor across files", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.SHADOW, project_id="p")
    assert decision.delegate is False
    assert "would have delegated" in decision.reason


def test_should_delegate_never_routes_solomon_own_project():
    env = InvocationEnvelope(request="please refactor across files", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.AUTO, project_id="solomon_ai_orchestrator")
    assert decision.delegate is False
    assert "self-referential" in decision.reason


def test_should_delegate_self_referential_overrides_force_solomon():
    env = InvocationEnvelope(request="anything", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.FORCE_SOLOMON, project_id="solomon_ai_orchestrator")
    assert decision.delegate is False


def test_should_delegate_no_signal_defaults_local():
    env = InvocationEnvelope(request="the weather is nice today", caller_type="claude_code")
    decision = should_delegate(env, GatewayMode.AUTO, project_id="p")
    assert decision.delegate is False
    assert decision.matched_signals == []


def test_build_return_contract_unknown_goal(tmp_path):
    store = make_store(tmp_path)
    contract = build_return_contract("no-such-goal", store)
    assert contract["status"] == "UNKNOWN"
    assert contract["tasks"] == []


def test_build_return_contract_assembles_real_data(tmp_path):
    store = make_store(tmp_path)
    goal = Goal(project_id="proj_a", text="ship the thing")
    store.save_goal(goal)
    task = Task(
        goal_id=goal.goal_id, project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.COMPLETE,
    )
    store.save_task(task)

    contract = build_return_contract(goal.goal_id, store)
    assert contract["goal_id"] == goal.goal_id
    assert contract["summary"] == "ship the thing"
    assert len(contract["tasks"]) == 1
    assert contract["tasks"][0]["task_id"] == task.task_id
    assert contract["verification_state"] == "VERIFIED"
    assert contract["usage"] is not None
