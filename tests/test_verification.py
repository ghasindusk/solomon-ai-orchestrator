import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.models import CommunicationMode, Risk, Task, TaskStatus
from solomon.policy import PolicyEngine
from solomon.registry import ProjectEntry, ProjectRegistry
from solomon.result import TaskResult
from solomon.state import StateStore
from solomon.verification import advance_after_result, verify_definition_of_done


class RequireDoDPolicy(PolicyEngine):
    def __init__(self, require: bool = True):
        self._policy = {"completion": {"require_applicable_definition_of_done": require}}


class FakeRegistry(ProjectRegistry):
    def __init__(self, projects):
        self._projects = {p.project_id: p for p in projects}


def make_task(dod, project_id="p1") -> Task:
    return Task(
        goal_id="g1", project_id=project_id, type="t", role="coder",
        definition_of_done=dod, status=TaskStatus.RESULT_RECEIVED,
    )


def record_result(store, task, status="RESULT_RECEIVED"):
    task.status = TaskStatus(status)
    store.save_task(task)
    store.save_result(
        TaskResult(
            task_id=task.task_id, status=status, summary="x", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
        )
    )


def test_result_recorded_satisfied_when_result_exists(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded"])
    record_result(store, task)
    v = verify_definition_of_done(task, store)
    assert v.satisfied == ["result_recorded"]
    assert not v.unsatisfied
    assert not v.unverifiable


def test_result_recorded_unsatisfied_when_no_result(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded"])
    store.save_task(task)  # no result saved
    v = verify_definition_of_done(task, store)
    assert v.unsatisfied == ["result_recorded"]


def test_unknown_criterion_is_unverifiable(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["something_solomon_cannot_check"])
    v = verify_definition_of_done(task, store)
    assert v.unverifiable == ["something_solomon_cannot_check"]
    assert not v.satisfied
    assert not v.unsatisfied


def test_review_of_satisfied_when_review_task_has_result(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    original = make_task(["result_recorded"])
    record_result(store, original)

    review = Task(
        goal_id="g1", project_id="p1", type="review", role="reviewer",
        definition_of_done=[f"review_of:{original.task_id}"],
        dependencies=[original.task_id], communication_mode=CommunicationMode.REVIEW,
    )
    record_result(store, review)

    original.definition_of_done = ["result_recorded", f"review_of:{original.task_id}"]
    v = verify_definition_of_done(original, store)
    assert set(v.satisfied) == {"result_recorded", f"review_of:{original.task_id}"}


def test_review_of_unsatisfied_when_no_review_task_exists(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    original = make_task(["result_recorded"])
    record_result(store, original)
    original.definition_of_done = [f"review_of:{original.task_id}"]
    v = verify_definition_of_done(original, store)
    assert v.unsatisfied == [f"review_of:{original.task_id}"]


def test_advance_promotes_to_complete_when_all_satisfied(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded"])
    record_result(store, task)
    status = advance_after_result(task, store, RequireDoDPolicy())
    assert status == TaskStatus.COMPLETE


def test_advance_returns_remediation_required_when_unsatisfied(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded"])
    store.save_task(task)  # no result
    status = advance_after_result(task, store, RequireDoDPolicy())
    assert status == TaskStatus.REMEDIATION_REQUIRED


def test_advance_stays_put_on_unverifiable_when_policy_requires_dod(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["some_unverifiable_thing"])
    status = advance_after_result(task, store, RequireDoDPolicy(require=True))
    assert status == TaskStatus.RESULT_RECEIVED


def test_advance_completes_despite_unverifiable_when_policy_does_not_require_dod(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["some_unverifiable_thing"])
    status = advance_after_result(task, store, RequireDoDPolicy(require=False))
    assert status == TaskStatus.COMPLETE


def test_advance_reverifies_remediation_required_task_and_promotes_to_complete(tmp_path):
    # Regression: a task stuck at REMEDIATION_REQUIRED (e.g. a HIGH-risk
    # task whose review didn't exist yet) must be re-promotable to
    # COMPLETE once the blocker (the review) is fixed -- verify-task's
    # whole reason to exist.
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded"])
    record_result(store, task)
    task.status = TaskStatus.REMEDIATION_REQUIRED  # as if a prior check failed
    status = advance_after_result(task, store, RequireDoDPolicy())
    assert status == TaskStatus.COMPLETE


def test_build_passes_unverifiable_when_no_build_command(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["build_passes"])
    registry = FakeRegistry([ProjectEntry(project_id="p1", name="p1", repo_path=str(tmp_path))])
    v = verify_definition_of_done(task, store, registry)
    assert v.unverifiable == ["build_passes"]


def test_build_passes_satisfied_when_command_succeeds(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["build_passes"])
    # `cd` with no args always exits 0 on Windows cmd.exe / POSIX shells alike
    registry = FakeRegistry(
        [ProjectEntry(project_id="p1", name="p1", repo_path=str(tmp_path), build_command="cd .")]
    )
    v = verify_definition_of_done(task, store, registry)
    assert v.satisfied == ["build_passes"]


def test_build_passes_unsatisfied_when_command_fails(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["build_passes"])
    registry = FakeRegistry(
        [ProjectEntry(project_id="p1", name="p1", repo_path=str(tmp_path), build_command="exit 1")]
    )
    v = verify_definition_of_done(task, store, registry)
    assert v.unsatisfied == ["build_passes"]


def test_build_passes_unsatisfied_on_timeout(tmp_path, monkeypatch):
    import subprocess as sp
    from solomon import verification as verification_module

    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["build_passes"])
    registry = FakeRegistry(
        [ProjectEntry(project_id="p1", name="p1", repo_path=str(tmp_path), build_command="anything")]
    )
    monkeypatch.setattr(
        verification_module.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired(cmd="anything", timeout=1)),
    )
    v = verify_definition_of_done(task, store, registry)
    assert v.unsatisfied == ["build_passes"]


def test_advance_completes_when_build_passes_satisfied(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded", "build_passes"])
    record_result(store, task)
    registry = FakeRegistry(
        [ProjectEntry(project_id="p1", name="p1", repo_path=str(tmp_path), build_command="cd .")]
    )
    status = advance_after_result(task, store, RequireDoDPolicy(), registry)
    assert status == TaskStatus.COMPLETE


def test_advance_is_noop_for_non_result_received_status(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task = make_task(["result_recorded"])
    task.status = TaskStatus.FAILED
    status = advance_after_result(task, store, RequireDoDPolicy())
    assert status == TaskStatus.FAILED
