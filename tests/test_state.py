import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.state import StateStore


def make_store(tmp_path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.sqlite3")


def test_acquire_locks_succeeds_when_free(tmp_path):
    store = make_store(tmp_path)
    ok = store.acquire_locks("task-1", "proj", [str(tmp_path / "a.py")])
    assert ok is True


def test_acquire_locks_conflicts_with_other_task(tmp_path):
    store = make_store(tmp_path)
    path = str(tmp_path / "a.py")
    assert store.acquire_locks("task-1", "proj", [path]) is True
    assert store.acquire_locks("task-2", "proj", [path]) is False


def test_acquire_locks_same_task_can_reacquire(tmp_path):
    store = make_store(tmp_path)
    path = str(tmp_path / "a.py")
    assert store.acquire_locks("task-1", "proj", [path]) is True
    assert store.acquire_locks("task-1", "proj", [path]) is True


def test_release_locks_frees_path_for_others(tmp_path):
    store = make_store(tmp_path)
    path = str(tmp_path / "a.py")
    assert store.acquire_locks("task-1", "proj", [path]) is True
    store.release_locks("task-1")
    assert store.acquire_locks("task-2", "proj", [path]) is True


def test_get_adapter_stats_no_history_returns_none_values(tmp_path):
    store = make_store(tmp_path)
    stats = store.get_adapter_stats("claude_code")
    assert stats == {"count": 0, "success_rate": None, "avg_duration_seconds": None}


def test_get_task_returns_none_for_unknown_id(tmp_path):
    store = make_store(tmp_path)
    assert store.get_task("no-such-task") is None


def test_list_events_records_and_filters_by_project(tmp_path):
    store = make_store(tmp_path)
    store.log_event(event="thing_happened", project_id="proj_a")
    store.log_event(event="other_thing", project_id="proj_b")

    all_events = store.list_events()
    assert len(all_events) == 2

    proj_a_events = store.list_events(project_id="proj_a")
    assert len(proj_a_events) == 1
    assert proj_a_events[0]["event"] == "thing_happened"


def test_reconcile_marks_stale_queued_task_unknown(tmp_path):
    from datetime import datetime, timedelta, timezone
    from solomon.models import Risk, Task, TaskStatus

    store = make_store(tmp_path)
    task = Task(
        goal_id="g1", project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.QUEUED,
    )
    store.save_task(task)

    # Not stale yet with a generous threshold.
    assert store.reconcile_stale_tasks(stale_after_minutes=30) == []
    assert store.get_task(task.task_id)["status"] == "QUEUED"

    # Simulate the process having died 60 minutes ago: reconcile "now" as
    # 60 minutes after the only event this task has (task_saved).
    future = datetime.now(timezone.utc) + timedelta(minutes=60)
    reconciled = store.reconcile_stale_tasks(stale_after_minutes=30, now=future)
    assert len(reconciled) == 1
    assert reconciled[0]["task_id"] == task.task_id
    assert reconciled[0]["status"] == "UNKNOWN"
    assert store.get_task(task.task_id)["status"] == "UNKNOWN"


def test_reconcile_ignores_terminal_status_tasks(tmp_path):
    from datetime import datetime, timedelta, timezone
    from solomon.models import Risk, Task, TaskStatus

    store = make_store(tmp_path)
    task = Task(
        goal_id="g1", project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.FAILED,
    )
    store.save_task(task)

    future = datetime.now(timezone.utc) + timedelta(minutes=60)
    assert store.reconcile_stale_tasks(stale_after_minutes=30, now=future) == []
    assert store.get_task(task.task_id)["status"] == "FAILED"


def test_list_events_v04_matches_event_schema_shape(tmp_path):
    store = make_store(tmp_path)
    store.log_event(
        event="task_result_recorded", project_id="proj_a", goal_id="goal-1",
        task_id="task-1", agent="claude_code", detail="RESULT_RECEIVED",
    )
    v04_events = store.list_events_v04(project_id="proj_a")
    assert len(v04_events) == 1
    event = v04_events[0]
    assert set(event.keys()) == {
        "event_id", "timestamp", "event_type", "project_id",
        "goal_id", "task_id", "agent_id", "payload",
    }
    assert event["event_id"].startswith("evt-")
    assert event["event_type"] == "task_result_recorded"
    assert event["agent_id"] == "claude_code"
    assert event["payload"] == {"detail": "RESULT_RECEIVED"}


def test_create_and_get_worktree_record(tmp_path):
    store = make_store(tmp_path)
    store.create_worktree_record("task-1", "proj", "/tmp/wt/task-1", "solomon/task-1", "main")
    wt = store.get_worktree("task-1")
    assert wt["status"] == "active"
    assert wt["branch"] == "solomon/task-1"
    assert wt["merged_at"] is None


def test_get_worktree_returns_none_for_unknown(tmp_path):
    store = make_store(tmp_path)
    assert store.get_worktree("no-such-task") is None


def test_update_worktree_status_sets_merged_at_on_merged(tmp_path):
    store = make_store(tmp_path)
    store.create_worktree_record("task-1", "proj", "/tmp/wt/task-1", "solomon/task-1", "main")
    store.update_worktree_status("task-1", "merged")
    wt = store.get_worktree("task-1")
    assert wt["status"] == "merged"
    assert wt["merged_at"] is not None


def test_update_worktree_status_other_status_does_not_set_merged_at(tmp_path):
    store = make_store(tmp_path)
    store.create_worktree_record("task-1", "proj", "/tmp/wt/task-1", "solomon/task-1", "main")
    store.update_worktree_status("task-1", "discarded")
    wt = store.get_worktree("task-1")
    assert wt["status"] == "discarded"
    assert wt["merged_at"] is None


def test_list_worktrees_filters_by_project_and_status(tmp_path):
    store = make_store(tmp_path)
    store.create_worktree_record("task-1", "proj_a", "/tmp/wt/task-1", "solomon/task-1", "main")
    store.create_worktree_record("task-2", "proj_b", "/tmp/wt/task-2", "solomon/task-2", "main")
    store.update_worktree_status("task-2", "merged")

    assert len(store.list_worktrees(project_id="proj_a")) == 1
    assert len(store.list_worktrees(status="merged")) == 1
    assert len(store.list_worktrees()) == 2


def test_get_usage_records_normalizes_task_results(tmp_path):
    from solomon.models import Task
    from solomon.result import TaskResult, Usage, UsageProvenance

    store = make_store(tmp_path)
    task = Task(goal_id="g1", project_id="proj-1", type="implement", role="coder", definition_of_done=["x"])
    store.save_task(task)
    result = TaskResult(
        task_id=task.task_id,
        status="RESULT_RECEIVED",
        summary="ok",
        agent="claude_code",
        started_at="2026-09-23T00:00:00+00:00",
        finished_at="2026-09-23T00:00:02+00:00",
        usage=Usage(input_tokens=100, output_tokens=50, provenance=UsageProvenance.API_REPORTED),
    )
    store.save_result(result)

    records = store.get_usage_records()
    assert len(records) == 1
    rec = records[0]
    assert rec.project_id == "proj-1"
    assert rec.goal_id == "g1"
    assert rec.task_id == task.task_id
    assert rec.agent_id == "claude_code"
    assert rec.provider == "anthropic"
    assert rec.tokens.input == 100
    assert rec.tokens.output == 50
    assert rec.tokens.total == 150
    assert rec.provenance == UsageProvenance.API_REPORTED


def test_get_usage_records_filters_by_project_id(tmp_path):
    from solomon.models import Task
    from solomon.result import TaskResult, Usage

    store = make_store(tmp_path)
    for project_id in ("proj-a", "proj-b"):
        task = Task(goal_id="g", project_id=project_id, type="t", role="coder", definition_of_done=["x"])
        store.save_task(task)
        store.save_result(TaskResult(
            task_id=task.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-09-23T00:00:00+00:00", finished_at="2026-09-23T00:00:01+00:00",
            usage=Usage(input_tokens=10, output_tokens=5),
        ))

    records = store.get_usage_records(project_id="proj-a")
    assert len(records) == 1
    assert records[0].project_id == "proj-a"


def test_reconcile_is_idempotent_second_pass_finds_nothing_new(tmp_path):
    """Phase 7 recovery test (DECISIONS.md D31): calling reconcile_stale_tasks
    twice in a row (e.g. two restarts close together) must not error or
    re-report a task it already resolved -- an UNKNOWN task is a terminal-
    enough state that in_flight_statuses no longer matches it."""
    from datetime import datetime, timedelta, timezone
    from solomon.models import Risk, Task, TaskStatus

    store = make_store(tmp_path)
    task = Task(
        goal_id="g1", project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.QUEUED,
    )
    store.save_task(task)

    future = datetime.now(timezone.utc) + timedelta(minutes=60)
    first_pass = store.reconcile_stale_tasks(stale_after_minutes=30, now=future)
    assert len(first_pass) == 1

    second_pass = store.reconcile_stale_tasks(stale_after_minutes=30, now=future)
    assert second_pass == []
    assert store.get_task(task.task_id)["status"] == "UNKNOWN"


def test_reconcile_respects_stale_after_minutes_boundary(tmp_path):
    """A task at exactly the threshold's edge: still-fresh stays untouched,
    one minute past it gets reconciled. Prevents an off-by-one regression
    from silently widening or narrowing the recovery window."""
    from datetime import datetime, timedelta, timezone
    from solomon.models import Risk, Task, TaskStatus

    store = make_store(tmp_path)
    task = Task(
        goal_id="g1", project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.QUEUED,
    )
    store.save_task(task)

    just_under = datetime.now(timezone.utc) + timedelta(minutes=29)
    assert store.reconcile_stale_tasks(stale_after_minutes=30, now=just_under) == []

    just_over = datetime.now(timezone.utc) + timedelta(minutes=31)
    reconciled = store.reconcile_stale_tasks(stale_after_minutes=30, now=just_over)
    assert len(reconciled) == 1


def test_reconcile_handles_crash_after_result_saved_before_status_synced(tmp_path):
    """Simulates a real interruption window: the adapter call finished and
    save_result() wrote task_results, but the process died before cli.py's
    subsequent save_task(status=RESULT_RECEIVED) ran -- tasks.status is
    still QUEUED even though a result exists. reconcile_stale_tasks must
    still mark it UNKNOWN (never silently infer RESULT_RECEIVED from the
    presence of a result -- that would be exactly the "fabricate an
    outcome" behavior D18 rejected for reconcile in general)."""
    from datetime import datetime, timedelta, timezone
    from solomon.models import Risk, Task, TaskStatus
    from solomon.result import TaskResult, Usage

    store = make_store(tmp_path)
    task = Task(
        goal_id="g1", project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.QUEUED,
    )
    store.save_task(task)
    store.save_result(
        TaskResult(
            task_id=task.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
    )
    # status was never synced -- still QUEUED despite a real result existing
    assert store.get_task(task.task_id)["status"] == "QUEUED"

    future = datetime.now(timezone.utc) + timedelta(minutes=60)
    reconciled = store.reconcile_stale_tasks(stale_after_minutes=30, now=future)
    assert len(reconciled) == 1
    assert reconciled[0]["status"] == "UNKNOWN"
    # the result itself is untouched -- reconcile only ever changes task status
    assert len(store.get_task_results(task.task_id)) == 1


def test_reconcile_does_not_release_file_locks(tmp_path):
    """Documents current, intentional behavior (D31): reconcile_stale_tasks
    marks a task UNKNOWN but does not release its file_locks. Auto-releasing
    locks based on an inactivity heuristic would risk letting a second task
    write to paths a first (merely slow, not actually dead) process still
    holds -- "Safety above autonomy" (README core principle) means this
    codebase prefers a human to resolve the ambiguity over guessing. If this
    behavior is ever intentionally changed, this test should be updated
    alongside that decision, not silently left failing."""
    from datetime import datetime, timedelta, timezone
    from solomon.models import Risk, Task, TaskStatus

    store = make_store(tmp_path)
    task = Task(
        goal_id="g1", project_id="proj_a", type="adhoc", role="coder",
        definition_of_done=[], risk=Risk.NORMAL, status=TaskStatus.QUEUED,
    )
    store.save_task(task)
    locked_path = str(tmp_path / "some_file.py")
    assert store.acquire_locks(task.task_id, "proj_a", [locked_path]) is True

    future = datetime.now(timezone.utc) + timedelta(minutes=60)
    store.reconcile_stale_tasks(stale_after_minutes=30, now=future)

    # lock is still held -- a second task cannot claim the same path
    assert store.acquire_locks("other-task", "proj_a", [locked_path]) is False
