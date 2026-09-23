"""Tests for the UsageRecord data model (v0.4 Phase 6 reopen step 2,
DECISIONS.md D27). Data-model-only: no adapter/state wiring yet."""

from solomon.models import Task
from solomon.result import TaskResult, Usage, UsageProvenance
from solomon.usage_record import (
    ComputeUsage,
    ContextSavings,
    DecisionUsage,
    LocalComputeUsage,
    QualityUsage,
    TokenUsage,
    UsageRecord,
    usage_record_from_task_result,
)


def _minimal_record(**overrides) -> UsageRecord:
    defaults = dict(
        record_id="rec-1",
        timestamp="2026-09-23T00:00:00+00:00",
        project_id="proj-1",
        agent_id="claude_code",
        provider="anthropic",
        model="claude-sonnet-5",
    )
    defaults.update(overrides)
    return UsageRecord(**defaults)


def test_required_fields_only_defaults_everything_else_to_unknown():
    rec = _minimal_record()
    assert rec.provenance == UsageProvenance.UNKNOWN
    assert rec.goal_id is None
    assert rec.task_id is None
    assert rec.session_id is None
    assert rec.tokens == TokenUsage()
    assert rec.savings == ContextSavings()
    assert rec.compute == ComputeUsage()
    assert rec.local_compute == LocalComputeUsage()
    assert rec.decision == DecisionUsage()
    assert rec.quality == QualityUsage()


def test_missing_values_are_none_never_coerced_to_zero():
    rec = _minimal_record()
    d = rec.to_dict()
    for field_name in ("input", "output", "cached_input", "context", "total"):
        assert d["tokens"][field_name] is None, field_name
    for field_name in (
        "raw_context_tokens",
        "sent_context_tokens",
        "context_saved_tokens",
        "reduction_ratio",
    ):
        assert d["savings"][field_name] is None, field_name


def test_to_dict_matches_usage_record_schema_shape():
    rec = _minimal_record(
        goal_id="goal-1",
        task_id="task-1",
        session_id="sess-1",
        role="implementer",
        tokens=TokenUsage(input=100, output=50, cached_input=10, context=200, total=150),
        savings=ContextSavings(
            raw_context_tokens=500,
            sent_context_tokens=200,
            context_saved_tokens=300,
            reduction_ratio=0.6,
        ),
        compute=ComputeUsage(duration_ms=1200, queue_ms=50, tokens_per_second=41.6),
        local_compute=LocalComputeUsage(gpu_time_ms=900, vram_peak_mb=4096, ram_peak_mb=8192),
        decision=DecisionUsage(
            decision_count=3,
            average_input_tokens_per_decision=25.0,
            decision_profile_result="approved",
        ),
        quality=QualityUsage(success=True, retry_count=0, review_result="PASSED", test_result="PASSED"),
        provenance=UsageProvenance.API_REPORTED,
    )
    d = rec.to_dict()
    assert set(d.keys()) == {
        "record_id",
        "timestamp",
        "project_id",
        "agent_id",
        "provider",
        "model",
        "provenance",
        "goal_id",
        "task_id",
        "session_id",
        "role",
        "tokens",
        "savings",
        "compute",
        "local_compute",
        "decision",
        "quality",
    }
    assert d["tokens"]["total"] == 150
    assert d["savings"]["reduction_ratio"] == 0.6
    assert d["decision"]["decision_count"] == 3


def test_from_dict_round_trips_through_to_dict():
    rec = _minimal_record(
        goal_id="goal-1",
        tokens=TokenUsage(input=10, output=20),
        provenance=UsageProvenance.ESTIMATED,
    )
    restored = UsageRecord.from_dict(rec.to_dict())
    assert restored == rec


def test_from_dict_tolerates_missing_optional_sections():
    raw = {
        "record_id": "rec-2",
        "timestamp": "2026-09-23T00:00:00+00:00",
        "project_id": "proj-1",
        "agent_id": "codex",
        "provider": "openai",
        "model": "gpt-x",
    }
    rec = UsageRecord.from_dict(raw)
    assert rec.provenance == UsageProvenance.UNKNOWN
    assert rec.tokens == TokenUsage()


def test_provenance_uses_shared_enum_values():
    assert UsageProvenance.API_REPORTED == "API_REPORTED"
    assert UsageProvenance.CLI_REPORTED == "CLI_REPORTED"
    assert UsageProvenance.CALCULATED == "CALCULATED"
    assert UsageProvenance.ESTIMATED == "ESTIMATED"
    assert UsageProvenance.UNKNOWN == "UNKNOWN"


def test_model_defaults_to_none_not_a_required_string():
    rec = UsageRecord(
        record_id="rec-3",
        timestamp="2026-09-23T00:00:00+00:00",
        project_id="proj-1",
        agent_id="antigravity",
        provider="google",
    )
    assert rec.model is None


def test_from_dict_tolerates_missing_model():
    raw = {
        "record_id": "rec-4",
        "timestamp": "2026-09-23T00:00:00+00:00",
        "project_id": "proj-1",
        "agent_id": "antigravity",
        "provider": "google",
    }
    rec = UsageRecord.from_dict(raw)
    assert rec.model is None


def _task(**overrides) -> Task:
    defaults = dict(
        goal_id="goal-1",
        project_id="proj-1",
        type="implement",
        role="implementer",
        definition_of_done=["tests pass"],
    )
    defaults.update(overrides)
    return Task(**defaults)


def _result(**overrides) -> TaskResult:
    defaults = dict(
        task_id="task-1",
        status="RESULT_RECEIVED",
        summary="did the thing",
        agent="claude_code",
        started_at="2026-09-23T00:00:00+00:00",
        finished_at="2026-09-23T00:00:02+00:00",
    )
    defaults.update(overrides)
    return TaskResult(**defaults)


def test_normalize_derives_provider_from_agent_id_when_not_given():
    task = _task()
    result = _result(agent="codex")
    rec = usage_record_from_task_result(task, result)
    assert rec.agent_id == "codex"
    assert rec.provider == "openai"


def test_normalize_falls_back_to_unknown_provider_for_unrecognized_agent():
    task = _task()
    result = _result(agent="some_future_adapter")
    rec = usage_record_from_task_result(task, result)
    assert rec.provider == "unknown"


def test_normalize_never_fabricates_model():
    task = _task()
    result = _result()
    rec = usage_record_from_task_result(task, result)
    assert rec.model is None


def test_normalize_copies_identity_from_task():
    task = _task(goal_id="goal-42", project_id="proj-42", role="reviewer", task_id="task-42")
    result = _result(task_id="task-42")
    rec = usage_record_from_task_result(task, result)
    assert rec.project_id == "proj-42"
    assert rec.goal_id == "goal-42"
    assert rec.task_id == "task-42"
    assert rec.role == "reviewer"


def test_normalize_session_id_stays_none_when_not_supplied():
    rec = usage_record_from_task_result(_task(), _result())
    assert rec.session_id is None


def test_normalize_session_id_passed_through_when_given():
    rec = usage_record_from_task_result(_task(), _result(), session_id="sess-1")
    assert rec.session_id == "sess-1"


def test_normalize_copies_tokens_and_computes_total():
    task = _task()
    result = _result()
    result.usage = Usage(input_tokens=100, output_tokens=50, context_tokens=20, provenance=UsageProvenance.API_REPORTED)
    rec = usage_record_from_task_result(task, result)
    assert rec.tokens.input == 100
    assert rec.tokens.output == 50
    assert rec.tokens.context == 20
    assert rec.tokens.total == 150
    assert rec.tokens.cached_input is None
    assert rec.provenance == UsageProvenance.API_REPORTED


def test_normalize_total_stays_none_when_either_side_missing():
    task = _task()
    result = _result()
    result.usage = Usage(input_tokens=100, output_tokens=None)
    rec = usage_record_from_task_result(task, result)
    assert rec.tokens.total is None


def test_normalize_computes_duration_and_tokens_per_second():
    task = _task()
    result = _result(started_at="2026-09-23T00:00:00+00:00", finished_at="2026-09-23T00:00:02+00:00")
    result.usage = Usage(output_tokens=100)
    rec = usage_record_from_task_result(task, result)
    assert rec.compute.duration_ms == 2000
    assert rec.compute.tokens_per_second == 50.0


def test_normalize_duration_stays_none_when_timestamps_unparseable():
    task = _task()
    result = _result(started_at="not-a-timestamp", finished_at="also-not-a-timestamp")
    rec = usage_record_from_task_result(task, result)
    assert rec.compute.duration_ms is None
    assert rec.compute.tokens_per_second is None


def test_normalize_leaves_savings_local_compute_decision_quality_unknown():
    rec = usage_record_from_task_result(_task(), _result())
    assert rec.savings == ContextSavings()
    assert rec.local_compute == LocalComputeUsage()
    assert rec.decision == DecisionUsage()
    assert rec.quality == QualityUsage()


def test_normalize_explicit_provider_and_model_override_defaults():
    rec = usage_record_from_task_result(
        _task(), _result(agent="localai_ollama"), provider="ollama", model="qwen2.5-coder:7b"
    )
    assert rec.provider == "ollama"
    assert rec.model == "qwen2.5-coder:7b"


def test_normalize_savings_defaults_to_unknown_when_not_given():
    rec = usage_record_from_task_result(_task(), _result())
    assert rec.savings == ContextSavings()


def test_normalize_savings_passed_through_when_given():
    savings = ContextSavings(
        raw_context_tokens=1000, sent_context_tokens=200, context_saved_tokens=800, reduction_ratio=0.8
    )
    rec = usage_record_from_task_result(_task(), _result(), savings=savings)
    assert rec.savings == savings
