"""Tests for token/compute aggregation across UsageRecords (Phase 6 reopen
step 4, DECISIONS.md D27)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.usage_aggregation import aggregate_usage_records
from solomon.usage_record import TokenUsage, UsageRecord


def _rec(**overrides) -> UsageRecord:
    defaults = dict(
        record_id="r",
        timestamp="2026-09-23T00:00:00+00:00",
        project_id="proj-1",
        agent_id="claude_code",
        provider="anthropic",
    )
    defaults.update(overrides)
    return UsageRecord(**defaults)


def test_rejects_unknown_axis():
    with pytest.raises(ValueError):
        aggregate_usage_records([], "not_a_real_axis")


def test_empty_input_gives_no_buckets():
    assert aggregate_usage_records([], "agent_id") == []


def test_groups_by_agent_id_and_sums_tokens():
    records = [
        _rec(agent_id="claude_code", tokens=TokenUsage(input=100, output=50, total=150)),
        _rec(agent_id="claude_code", tokens=TokenUsage(input=200, output=100, total=300)),
        _rec(agent_id="codex", tokens=TokenUsage(input=10, output=5, total=15)),
    ]
    buckets = {b.key: b for b in aggregate_usage_records(records, "agent_id")}
    assert buckets["claude_code"].record_count == 2
    assert buckets["claude_code"].total_tokens == 450
    assert buckets["claude_code"].total_input_tokens == 300
    assert buckets["codex"].total_tokens == 15


def test_unknown_totals_are_not_zero_filled_and_are_counted():
    records = [
        _rec(agent_id="a", tokens=TokenUsage(total=100)),
        _rec(agent_id="a", tokens=TokenUsage(total=None)),  # UNKNOWN, not 0
    ]
    bucket = aggregate_usage_records(records, "agent_id")[0]
    assert bucket.record_count == 2
    assert bucket.total_tokens == 100  # sum of the known one only
    assert bucket.known_total_count == 1
    assert bucket.unknown_total_count == 1


def test_all_unknown_totals_gives_none_not_zero():
    records = [_rec(tokens=TokenUsage(total=None)), _rec(tokens=TokenUsage(total=None))]
    bucket = aggregate_usage_records(records, "agent_id")[0]
    assert bucket.total_tokens is None
    assert bucket.unknown_total_count == 2


def test_day_and_month_axes_bucket_by_timestamp_prefix():
    records = [
        _rec(timestamp="2026-09-23T10:00:00+00:00"),
        _rec(timestamp="2026-09-23T15:00:00+00:00"),
        _rec(timestamp="2026-08-01T00:00:00+00:00"),
    ]
    days = {b.key: b.record_count for b in aggregate_usage_records(records, "day")}
    assert days == {"2026-09-23": 2, "2026-08-01": 1}

    months = {b.key: b.record_count for b in aggregate_usage_records(records, "month")}
    assert months == {"2026-09": 2, "2026-08": 1}


def test_provenance_counts_and_has_estimated_flag():
    from solomon.result import UsageProvenance

    records = [
        _rec(provenance=UsageProvenance.API_REPORTED),
        _rec(provenance=UsageProvenance.ESTIMATED),
        _rec(provenance=UsageProvenance.ESTIMATED),
    ]
    bucket = aggregate_usage_records(records, "agent_id")[0]
    assert bucket.provenance_counts == {"API_REPORTED": 1, "ESTIMATED": 2}
    assert bucket.has_estimated is True
    assert bucket.has_unknown_provenance is False


def test_session_id_axis_groups_unset_sessions_under_none_key():
    records = [_rec(session_id="sess-1"), _rec(session_id=None)]
    buckets = {b.key: b.record_count for b in aggregate_usage_records(records, "session_id")}
    assert buckets == {"sess-1": 1, None: 1}
