"""Tests for Token Efficiency computation (Phase 6 reopen step 6,
DECISIONS.md D27)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.token_efficiency import compute_token_efficiency
from solomon.usage_record import TokenUsage, UsageRecord


def _rec(task_id: str, total: int | None, **overrides) -> UsageRecord:
    defaults = dict(
        record_id="r",
        timestamp="2026-09-23T00:00:00+00:00",
        project_id="proj-1",
        agent_id="claude_code",
        provider="anthropic",
        task_id=task_id,
        tokens=TokenUsage(total=total),
    )
    defaults.update(overrides)
    return UsageRecord(**defaults)


def test_empty_records_gives_zero_task_count_and_none_metrics():
    eff = compute_token_efficiency([], success_task_ids=set(), verified_task_ids=set())
    assert eff.task_count == 0
    assert eff.tokens_per_task is None
    assert eff.tokens_per_success is None
    assert eff.tokens_per_verified_success is None
    assert eff.retry_token_overhead is None


def test_tokens_per_task_averages_across_all_tasks():
    records = [_rec("t1", 100), _rec("t2", 300)]
    eff = compute_token_efficiency(records, success_task_ids=set(), verified_task_ids=set())
    assert eff.task_count == 2
    assert eff.tokens_per_task == 200.0


def test_tokens_per_success_only_averages_successful_tasks():
    records = [_rec("t1", 100), _rec("t2", 300), _rec("t3", 50)]
    eff = compute_token_efficiency(records, success_task_ids={"t1", "t2"}, verified_task_ids=set())
    assert eff.success_task_count == 2
    assert eff.tokens_per_success == 200.0  # (100+300)/2, t3 excluded


def test_tokens_per_verified_success_is_stricter_subset_of_success():
    records = [_rec("t1", 100), _rec("t2", 300)]
    eff = compute_token_efficiency(records, success_task_ids={"t1", "t2"}, verified_task_ids={"t1"})
    assert eff.verified_success_task_count == 1
    assert eff.tokens_per_verified_success == 100.0
    assert eff.tokens_per_success == 200.0


def test_retry_overhead_only_counts_tasks_with_multiple_attempts():
    records = [
        _rec("t1", 50),  # single attempt, no retry
        _rec("t2", 80),  # first attempt of t2
        _rec("t2", 120),  # retry of t2, total across both = 200
    ]
    eff = compute_token_efficiency(records, success_task_ids=set(), verified_task_ids=set())
    assert eff.retried_task_count == 1
    # t2 total = 80 + 120 = 200, first attempt = 80, overhead = 120
    assert eff.retry_token_overhead == 120.0


def test_unknown_totals_are_excluded_not_zero_filled():
    records = [_rec("t1", 100), _rec("t2", None)]
    eff = compute_token_efficiency(records, success_task_ids=set(), verified_task_ids=set())
    assert eff.task_count == 2  # both tasks counted
    assert eff.tokens_per_task == 100.0  # only t1 contributes to the average


def test_all_unknown_totals_gives_none_not_zero():
    records = [_rec("t1", None), _rec("t2", None)]
    eff = compute_token_efficiency(records, success_task_ids=set(), verified_task_ids=set())
    assert eff.task_count == 2
    assert eff.tokens_per_task is None


def test_records_without_task_id_are_ignored():
    records = [_rec("t1", 100), UsageRecord(
        record_id="r2", timestamp="2026-09-23T00:00:00+00:00", project_id="proj-1",
        agent_id="claude_code", provider="anthropic", task_id=None, tokens=TokenUsage(total=999),
    )]
    eff = compute_token_efficiency(records, success_task_ids=set(), verified_task_ids=set())
    assert eff.task_count == 1
    assert eff.tokens_per_task == 100.0


def test_scope_and_scope_id_are_carried_through():
    eff = compute_token_efficiency(
        [_rec("t1", 100)], success_task_ids=set(), verified_task_ids=set(), scope="project", scope_id="proj-1"
    )
    assert eff.scope == "project"
    assert eff.scope_id == "proj-1"
