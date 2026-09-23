import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.models import CommunicationMode, Risk, Task, TaskStatus, task_status_from_result_status


def test_result_received_maps_to_result_received():
    assert task_status_from_result_status("RESULT_RECEIVED") == TaskStatus.RESULT_RECEIVED


def test_failed_maps_to_failed():
    assert task_status_from_result_status("FAILED") == TaskStatus.FAILED


def test_never_jumps_straight_to_complete():
    # Agent output is evidence, not completion (spec section 6) -- no
    # result status should ever produce TaskStatus.COMPLETE here, since
    # that requires a Definition-of-Done check this module doesn't do.
    for status in ("RESULT_RECEIVED", "FAILED", "anything_else"):
        assert task_status_from_result_status(status) != TaskStatus.COMPLETE


def test_schema_dict_round_trip_preserves_all_fields():
    original = Task(
        goal_id="g1", project_id="p1", type="adhoc", role="coder",
        definition_of_done=["result_recorded", "review_of:task-abc"],
        risk=Risk.HIGH, status=TaskStatus.REMEDIATION_REQUIRED,
        dependencies=["task-x"], context_budget_tokens=1234,
        assigned_agent="claude_code", communication_mode=CommunicationMode.REVIEW,
        autonomy=3,
    )
    restored = Task.from_schema_dict(original.to_schema_dict())
    assert restored.to_schema_dict() == original.to_schema_dict()


def test_autonomy_defaults_to_none():
    task = Task(goal_id="g1", project_id="p1", type="adhoc", role="coder", definition_of_done=[])
    assert task.autonomy is None
    assert task.to_schema_dict()["autonomy"] is None


def test_autonomy_out_of_range_rejected():
    import pytest

    for bad in (-1, 6):
        with pytest.raises(ValueError):
            Task(goal_id="g1", project_id="p1", type="adhoc", role="coder", definition_of_done=[], autonomy=bad)
