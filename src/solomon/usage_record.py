"""UsageRecord data model (v0.4 Token & Compute Intelligence, step 2/10 of
the Phase 6 reopen -- see DECISIONS.md D27).

Mirrors 07_Schemas/usage_record.schema.json field-for-field. This module
defines the *normalized*, storage-level record only. Provider adapters keep
returning the existing (Phase 4) `Usage` dataclass from result.py unchanged;
normalizing a TaskResult + its Usage into a UsageRecord is step 3.

Every numeric field defaults to None, meaning UNKNOWN -- never 0. A value
that cannot be obtained must stay None rather than being coerced, per the
spec principle "取得不能な値を0として扱わない" (Formal Spec v0.4 section
17.1 / additional spec section 2).

Provenance describes the reliability of THIS record's numeric values as a
whole (API_REPORTED > CLI_REPORTED > CALCULATED > ESTIMATED > UNKNOWN); it
reuses result.UsageProvenance rather than redefining the same five
constants twice.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .result import UsageProvenance

if TYPE_CHECKING:
    from .models import Task
    from .result import TaskResult

__all__ = [
    "UsageProvenance",
    "usage_record_from_task_result",
    "TokenUsage",
    "ContextSavings",
    "ComputeUsage",
    "LocalComputeUsage",
    "DecisionUsage",
    "QualityUsage",
    "UsageRecord",
]


@dataclass
class TokenUsage:
    input: int | None = None
    output: int | None = None
    cached_input: int | None = None
    context: int | None = None
    total: int | None = None


@dataclass
class ContextSavings:
    raw_context_tokens: int | None = None
    sent_context_tokens: int | None = None
    context_saved_tokens: int | None = None
    reduction_ratio: float | None = None


@dataclass
class ComputeUsage:
    duration_ms: int | None = None
    queue_ms: int | None = None
    tokens_per_second: float | None = None


@dataclass
class LocalComputeUsage:
    gpu_time_ms: int | None = None
    vram_peak_mb: int | None = None
    ram_peak_mb: int | None = None


@dataclass
class DecisionUsage:
    """Jev-style Decision Intelligence usage, tracked separately from
    generation-agent token usage (additional spec section 9)."""

    decision_count: int | None = None
    average_input_tokens_per_decision: float | None = None
    decision_profile_result: str | None = None


@dataclass
class QualityUsage:
    success: bool | None = None
    retry_count: int | None = None
    review_result: str | None = None
    test_result: str | None = None


@dataclass
class UsageRecord:
    record_id: str
    timestamp: str
    project_id: str
    agent_id: str
    provider: str
    provenance: str = UsageProvenance.UNKNOWN
    goal_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    role: str | None = None
    # Unlike provider (derivable from agent_id, see _PROVIDER_BY_AGENT),
    # today's adapters mostly do not surface which specific model served a
    # task (only ollama_adapter tracks one, and it isn't threaded through
    # TaskResult). Optional/None here rather than a required field, so
    # normalization never has to fabricate a model string.
    model: str | None = None
    tokens: TokenUsage = field(default_factory=TokenUsage)
    savings: ContextSavings = field(default_factory=ContextSavings)
    compute: ComputeUsage = field(default_factory=ComputeUsage)
    local_compute: LocalComputeUsage = field(default_factory=LocalComputeUsage)
    decision: DecisionUsage = field(default_factory=DecisionUsage)
    quality: QualityUsage = field(default_factory=QualityUsage)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "UsageRecord":
        data = dict(data)
        return UsageRecord(
            record_id=data["record_id"],
            timestamp=data["timestamp"],
            project_id=data["project_id"],
            agent_id=data["agent_id"],
            provider=data["provider"],
            provenance=data.get("provenance", UsageProvenance.UNKNOWN),
            goal_id=data.get("goal_id"),
            task_id=data.get("task_id"),
            session_id=data.get("session_id"),
            role=data.get("role"),
            model=data.get("model"),
            tokens=TokenUsage(**(data.get("tokens") or {})),
            savings=ContextSavings(**(data.get("savings") or {})),
            compute=ComputeUsage(**(data.get("compute") or {})),
            local_compute=LocalComputeUsage(**(data.get("local_compute") or {})),
            decision=DecisionUsage(**(data.get("decision") or {})),
            quality=QualityUsage(**(data.get("quality") or {})),
        )


# agent_id -> provider is well-known, static adapter metadata (which
# company/runtime backs each adapter), not measured usage data -- safe to
# hardcode, unlike model (see UsageRecord.model docstring above).
_PROVIDER_BY_AGENT = {
    "claude_code": "anthropic",
    "codex": "openai",
    "antigravity": "google",
    "localai_ollama": "ollama",
}


def _duration_ms(started_at: str | None, finished_at: str | None) -> int | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except (TypeError, ValueError):
        return None
    delta_ms = (end - start).total_seconds() * 1000
    return int(delta_ms) if delta_ms >= 0 else None


def usage_record_from_task_result(
    task: "Task",
    result: "TaskResult",
    *,
    provider: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
    savings: ContextSavings | None = None,
) -> UsageRecord:
    """Normalize a Provider Adapter's TaskResult (and its embedded Usage)
    into a UsageRecord (Phase 6 reopen step 3/10, DECISIONS.md D27).

    Populates identity, agent/provider/model, tokens and duration/
    tokens_per_second only -- those are the fields derivable from what
    adapters already report today. savings/local_compute/decision/quality
    stay at their UNKNOWN defaults here; wiring Context Firewall savings,
    local compute telemetry and Jev decision usage in is step 5.

    `session_id` is the caller's session (gateway.InvocationEnvelope.
    caller_session_id) when this task was reached via the gateway; pass it
    through explicitly rather than guessing -- a directly-run task (CLI
    `run-task`) genuinely has no session, and that must stay None/UNKNOWN,
    not be invented.

    `savings` (step 5, D27): pass knowledge.context_savings(pack,
    raw_context_tokens) when the Context Firewall built a ContextPack for
    this task's context; defaults to all-UNKNOWN when not given (e.g. a
    task with no knowledge retrieval step at all).
    """
    usage = result.usage
    input_tok = usage.input_tokens
    output_tok = usage.output_tokens
    total_tok = input_tok + output_tok if input_tok is not None and output_tok is not None else None

    duration_ms = _duration_ms(result.started_at, result.finished_at)
    tokens_per_second = None
    if duration_ms and duration_ms > 0 and output_tok:
        tokens_per_second = output_tok / (duration_ms / 1000)

    return UsageRecord(
        record_id=f"usage-{uuid.uuid4().hex[:12]}",
        timestamp=result.finished_at,
        project_id=task.project_id,
        goal_id=task.goal_id,
        task_id=task.task_id,
        session_id=session_id,
        agent_id=result.agent,
        provider=provider or _PROVIDER_BY_AGENT.get(result.agent, "unknown"),
        model=model,
        role=task.role,
        provenance=usage.provenance,
        tokens=TokenUsage(
            input=input_tok,
            output=output_tok,
            cached_input=None,
            context=usage.context_tokens,
            total=total_tok,
        ),
        compute=ComputeUsage(duration_ms=duration_ms, tokens_per_second=tokens_per_second),
        savings=savings if savings is not None else ContextSavings(),
    )
