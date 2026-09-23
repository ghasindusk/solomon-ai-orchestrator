"""Structured Task Result contract (Architecture doc section 4).

Required: task_id, status, summary, artifacts, changed_files, uncertainties,
evidence, usage, timestamps.
Optional: tests, review_findings, recommended_next_tasks, decisions_proposed.

Agent output is evidence, not completion (spec section 6) -- Solomon (the
Director) still owns the state transition after reviewing a TaskResult.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


class UsageProvenance:
    API_REPORTED = "API_REPORTED"
    CLI_REPORTED = "CLI_REPORTED"
    CALCULATED = "CALCULATED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    context_tokens: int | None = None
    cost_usd: float | None = None
    provenance: str = UsageProvenance.UNKNOWN


@dataclass
class TaskResult:
    task_id: str
    status: str
    summary: str
    agent: str
    started_at: str
    finished_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    artifacts: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tests: list[str] | None = None
    review_findings: list[str] | None = None
    recommended_next_tasks: list[str] | None = None
    decisions_proposed: list[str] | None = None
    raw_output: str | None = None
    returncode: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
