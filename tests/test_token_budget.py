"""Tests for the Token Budget manager (Phase 6 reopen step 4, DECISIONS.md
D27). Parallel to test_usage.py's USD-based budget tests, but token-counted
and multi-scope (global/project/agent/model/goal/task)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.token_budget import TokenBudgetManager
from solomon.usage_record import TokenUsage, UsageRecord


def _rec(**overrides) -> UsageRecord:
    defaults = dict(
        record_id="r",
        timestamp="2026-09-23T00:00:00+00:00",
        project_id="proj-1",
        agent_id="claude_code",
        provider="anthropic",
        tokens=TokenUsage(total=100),
    )
    defaults.update(overrides)
    return UsageRecord(**defaults)


def write_config(tmp_path, **budget_overrides) -> pathlib.Path:
    p = tmp_path / "token_budget.yaml"
    budget = {"limit_tokens": 1000, "window": "all_time", "warning_percent": 70, "critical_percent": 90, "hard_stop_percent": 100}
    budget.update(budget_overrides)
    import yaml

    p.write_text(
        yaml.safe_dump(
            {
                "version": 0.4,
                "budgets": {"global": budget, "scopes": {"project": {}, "agent": {}, "model": {}, "goal": {}, "task": {}}},
                "actions": {"warning": ["log"], "critical": ["compress_context"], "hard_stop": ["pause_or_request_approval"]},
            }
        ),
        encoding="utf-8",
    )
    return p


def test_no_records_gives_unknown_status(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([], scope="global")
    assert status.level == "unknown"
    assert status.pct_used is None
    assert status.total_tokens is None


def test_ok_below_warning_threshold(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([_rec(tokens=TokenUsage(total=100))], scope="global")
    assert status.total_tokens == 100
    assert status.level == "ok"


def test_crosses_into_critical(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([_rec(tokens=TokenUsage(total=920))], scope="global")
    assert status.level == "critical"
    assert "compress_context" in status.actions


def test_hard_stop_at_limit(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([_rec(tokens=TokenUsage(total=1000))], scope="global")
    assert status.level == "hard_stop"
    assert "pause_or_request_approval" in status.actions


def test_unknown_totals_never_treated_as_zero_or_counted_toward_usage(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    records = [_rec(tokens=TokenUsage(total=500)), _rec(tokens=TokenUsage(total=None))]
    status = manager.check(records, scope="global")
    assert status.total_tokens == 500
    assert status.known_record_count == 1
    assert status.unknown_record_count == 1


def test_project_scope_filters_to_matching_records_only(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    records = [
        _rec(project_id="proj-a", tokens=TokenUsage(total=100)),
        _rec(project_id="proj-b", tokens=TokenUsage(total=900)),
    ]
    status_a = manager.check(records, scope="project", scope_id="proj-a")
    assert status_a.total_tokens == 100

    status_b = manager.check(records, scope="project", scope_id="proj-b")
    assert status_b.total_tokens == 900


def test_agent_and_goal_and_task_scopes_also_filter(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    records = [
        _rec(agent_id="claude_code", goal_id="g1", task_id="t1", tokens=TokenUsage(total=50)),
        _rec(agent_id="codex", goal_id="g2", task_id="t2", tokens=TokenUsage(total=999)),
    ]
    assert manager.check(records, scope="agent", scope_id="claude_code").total_tokens == 50
    assert manager.check(records, scope="goal", scope_id="g2").total_tokens == 999
    assert manager.check(records, scope="task", scope_id="t1").total_tokens == 50


def test_rejects_unknown_scope(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path))
    with pytest.raises(ValueError):
        manager.check([], scope="not_a_real_scope")


def test_project_override_takes_precedence_over_global(tmp_path):
    p = write_config(tmp_path, limit_tokens=1000)
    import yaml

    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["budgets"]["scopes"]["project"]["proj-a"] = {"limit_tokens": 10000}
    p.write_text(yaml.safe_dump(data), encoding="utf-8")

    manager = TokenBudgetManager(p)
    status = manager.check([_rec(project_id="proj-a", tokens=TokenUsage(total=900))], scope="project", scope_id="proj-a")
    assert status.level == "ok"  # 900/10000 = 9%, would be critical against the global 1000 limit


def test_daily_window_excludes_older_records(tmp_path):
    manager = TokenBudgetManager(write_config(tmp_path, window="daily"))
    old_record = _rec(timestamp="2020-01-01T00:00:00+00:00", tokens=TokenUsage(total=999))
    status = manager.check([old_record], scope="global")
    assert status.total_tokens is None  # excluded by the daily window -> no known records


def test_hard_stop_approval_reason_none_below_hard_stop(tmp_path):
    from solomon.token_budget import hard_stop_approval_reason

    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([_rec(tokens=TokenUsage(total=100))], scope="global")
    assert hard_stop_approval_reason(status) is None


def test_hard_stop_approval_reason_none_when_unknown(tmp_path):
    from solomon.token_budget import hard_stop_approval_reason

    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([], scope="global")
    assert status.level == "unknown"
    assert hard_stop_approval_reason(status) is None


def test_hard_stop_approval_reason_present_at_hard_stop(tmp_path):
    from solomon.token_budget import hard_stop_approval_reason

    manager = TokenBudgetManager(write_config(tmp_path))
    status = manager.check([_rec(project_id="proj-x", tokens=TokenUsage(total=1000))], scope="project", scope_id="proj-x")
    reason = hard_stop_approval_reason(status)
    assert reason is not None
    assert "proj-x" in reason
    assert "1000" in reason
