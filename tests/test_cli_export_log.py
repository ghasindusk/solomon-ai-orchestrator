"""Tests for cli._render_execution_log_markdown (export-log secret
redaction, v0.4 Phase 9 follow-up, DECISIONS.md D36). Formal Spec v0.4
section 21's privacy rule applies to any exported record, not only
`diagnostics-export`; export-log's job is a readable local project
record (not omit-by-default), so these tests check that embedded
credential-shaped text is stripped while ordinary content is kept."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.cli import _render_execution_log_markdown

_SECRET = "sk-" + "A" * 25


def _task(task_id="task-1", status="COMPLETE", role="coder", project_id="proj-1", risk="NORMAL"):
    return {"task_id": task_id, "status": status, "role": role, "project_id": project_id, "risk": risk}


def _result(agent="claude_code", status="RESULT_RECEIVED", summary="ok", cost_usd=None):
    return {"agent": agent, "status": status, "summary": summary, "usage": {"cost_usd": cost_usd}}


def _approval(request_id="appr-1", status="approved", risk="HIGH", reason="did the thing"):
    return {"request_id": request_id, "status": status, "risk": risk, "reason": reason}


def _event(ts="2026-09-24T00:00:00+00:00", event="task_saved", task_id="task-1", agent=None, detail="QUEUED"):
    return {"ts": ts, "event": event, "task_id": task_id, "agent": agent, "detail": detail}


def test_ordinary_content_is_preserved_not_stripped():
    task = _task()
    md = _render_execution_log_markdown(
        "proj-1", [task], [], [], {task["task_id"]: [_result(summary="deleted the old cache dir")]},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert "deleted the old cache dir" in md
    assert "claude_code" in md
    assert "task-1" in md


def test_secret_redacted_from_task_result_summary():
    task = _task()
    md = _render_execution_log_markdown(
        "proj-1", [task], [], [], {task["task_id"]: [_result(summary=f"used key {_SECRET} to auth")]},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert _SECRET not in md
    assert "[REDACTED]" in md
    assert "used key" in md  # surrounding text preserved


def test_secret_redacted_from_approval_reason():
    md = _render_execution_log_markdown(
        "proj-1", [], [], [_approval(reason=f"prompt included {_SECRET} accidentally")], {},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert _SECRET not in md
    assert "[REDACTED]" in md
    assert "prompt included" in md


def test_secret_redacted_from_event_detail():
    md = _render_execution_log_markdown(
        "proj-1", [], [_event(detail=f"payload contained {_SECRET}")], [], {},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert _SECRET not in md
    assert "[REDACTED]" in md


def test_none_event_detail_does_not_crash():
    md = _render_execution_log_markdown(
        "proj-1", [], [_event(detail=None)], [], {},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert "detail=" in md


def test_task_with_no_results_still_renders():
    task = _task(task_id="task-empty")
    md = _render_execution_log_markdown(
        "proj-1", [task], [], [], {},  # no entry for task-empty in task_results_by_id
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert "task-empty" in md


def test_cost_none_renders_as_n_a():
    task = _task()
    md = _render_execution_log_markdown(
        "proj-1", [task], [], [], {task["task_id"]: [_result(cost_usd=None)]},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert "cost=n/a" in md


def test_project_scoping_is_the_callers_responsibility_and_data_is_not_mixed():
    """_render_execution_log_markdown itself has no project_id filter --
    same architecture as knowledge.search_notes (D31 finding): isolation
    comes from the caller only ever passing one project's rows in. This
    test locks in that whatever is passed through renders faithfully
    (no cross-contamination introduced by the renderer itself)."""
    task_a = _task(task_id="task-a", project_id="proj-a")
    md = _render_execution_log_markdown(
        "proj-a", [task_a], [], [], {"task-a": [_result(summary="proj-a only content")]},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert "proj-a only content" in md
    assert "proj-b" not in md


def test_multiple_secrets_in_different_sections_all_redacted():
    task = _task()
    md = _render_execution_log_markdown(
        "proj-1",
        [task],
        [_event(detail=f"event has {_SECRET}")],
        [_approval(reason=f"approval has {_SECRET}")],
        {task["task_id"]: [_result(summary=f"summary has {_SECRET}")]},
        generated_at="2026-09-24T00:00:00+00:00",
    )
    assert _SECRET not in md
    assert md.count("[REDACTED]") == 3
