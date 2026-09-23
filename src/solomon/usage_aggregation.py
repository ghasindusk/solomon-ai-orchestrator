"""Token/Compute aggregation across UsageRecords (v0.4 Token & Compute
Intelligence, Phase 6 reopen step 4/10 -- DECISIONS.md D27).

Additional spec section 5 / Formal Spec v0.4 section 17.4 require
aggregation along Agent -> Model -> Project -> Goal -> Task -> Session ->
Day -> Month. This module buckets a list of UsageRecords by one of those
axes and sums their token fields.

Every bucket reports known_record_count/unknown_record_count alongside its
totals, and total_tokens is the sum over records that HAD a value -- never
zero-filled and never silently presented as a complete total when some
records are UNKNOWN. Callers that display a total must also surface
unknown_record_count (see Formal Spec v0.4 section 17.1: "推定値を実測値と
混在させて『正確な総計』として表示しない" / "取得不能な値を0として扱わない").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .usage_record import UsageRecord

__all__ = ["UsageAggregate", "aggregate_usage_records", "VALID_AXES"]

VALID_AXES = ("agent_id", "provider", "model", "project_id", "goal_id", "task_id", "session_id", "day", "month")


def _bucket_key(record: UsageRecord, axis: str) -> str | None:
    if axis == "day":
        return record.timestamp[:10] if record.timestamp else None
    if axis == "month":
        return record.timestamp[:7] if record.timestamp else None
    return getattr(record, axis)


@dataclass
class UsageAggregate:
    axis: str
    key: str | None  # None means "records with this axis value UNKNOWN" (e.g. session_id)
    record_count: int = 0
    known_input_count: int = 0
    known_output_count: int = 0
    known_total_count: int = 0
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    provenance_counts: dict[str, int] = field(default_factory=dict)

    @property
    def unknown_total_count(self) -> int:
        return self.record_count - self.known_total_count

    @property
    def has_estimated(self) -> bool:
        return self.provenance_counts.get("ESTIMATED", 0) > 0

    @property
    def has_unknown_provenance(self) -> bool:
        return self.provenance_counts.get("UNKNOWN", 0) > 0


def aggregate_usage_records(records: list[UsageRecord], axis: str) -> list[UsageAggregate]:
    if axis not in VALID_AXES:
        raise ValueError(f"unknown aggregation axis: {axis!r}, expected one of {VALID_AXES}")

    buckets: dict[str | None, UsageAggregate] = {}
    for record in records:
        key = _bucket_key(record, axis)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = UsageAggregate(axis=axis, key=key)
            buckets[key] = bucket

        bucket.record_count += 1
        bucket.provenance_counts[record.provenance] = bucket.provenance_counts.get(record.provenance, 0) + 1

        if record.tokens.input is not None:
            bucket.total_input_tokens = (bucket.total_input_tokens or 0) + record.tokens.input
            bucket.known_input_count += 1
        if record.tokens.output is not None:
            bucket.total_output_tokens = (bucket.total_output_tokens or 0) + record.tokens.output
            bucket.known_output_count += 1
        if record.tokens.total is not None:
            bucket.total_tokens = (bucket.total_tokens or 0) + record.tokens.total
            bucket.known_total_count += 1

    return list(buckets.values())
