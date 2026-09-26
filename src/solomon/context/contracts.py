"""Strict, bounded immutable wire records; validation never grants authority."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from jsonschema import Draft202012Validator, FormatChecker

MAX_BYTES = 262144


class ContractError(ValueError):
    pass


def identifier(value, label="record"):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ContractError(f"invalid {label} ID")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def _reject_number(value):
    raise ContractError("floating point and non-finite numbers are forbidden")


def decode(raw: str | bytes) -> dict:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        elif not isinstance(raw, str):
            raise ContractError("JSON text or bytes required")
        if len(raw.encode("utf-8")) > MAX_BYTES:
            raise ContractError("record too large")
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_float=_reject_number, parse_constant=_reject_number)
        if not isinstance(value, dict):
            raise ContractError("object required")
        return value
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise ContractError("invalid or unbounded JSON record") from exc


def canonical(value: dict) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        decode(raw)
        return raw
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid canonical value") from exc


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=7)
def validator(kind: str):
    if kind not in RECORD_TYPES:
        raise ContractError("unknown record kind")
    path = Path(__file__).parent / "schemas" / (kind + ".schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _bounds(value, depth=0):
    if depth > 20:
        raise ContractError("nesting limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_at") or key == "valid_until":
                if item is not None:
                    if not isinstance(item, str) or not re.fullmatch(
                            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", item):
                        raise ContractError("UTC RFC3339 timestamp required")
                    try:
                        datetime.fromisoformat(item)
                    except ValueError as exc:
                        raise ContractError("invalid timestamp") from exc
            if (key.endswith("_id") or key.endswith("_version") or
                    key == "expected_revision") and item is not None:
                identifier(item)
            if (key.endswith("_ids") or key == "derived_from") and isinstance(item, list):
                for reference in item:
                    identifier(reference, "reference")
            _bounds(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise ContractError("array limit")
        for item in value:
            _bounds(item, depth + 1)


@dataclass(frozen=True)
class Record:
    """Canonical JSON is the immutable backing; data returns a defensive copy."""
    raw: str
    kind: ClassVar[str]
    id_field: ClassVar[str]

    def __post_init__(self):
        value = decode(self.raw)
        _bounds(value)
        if not validator(self.kind).is_valid(value):
            # Do not include source content in exception/log text.
            raise ContractError("record violates " + self.kind + " schema")
        self._semantic(value)
        object.__setattr__(self, "raw", canonical(value))

    @classmethod
    def from_dict(cls, value: dict):
        return cls(canonical(value))

    @property
    def data(self) -> dict:
        return decode(self.raw)

    @property
    def record_id(self) -> str:
        return self.data[self.id_field]

    @property
    def project_id(self) -> str:
        if "project_id" not in self.data:
            raise ContractError("record is not project scoped")
        return self.data["project_id"]

    def _semantic(self, value):
        for earlier, later in (("source_created_at", "captured_at"),
                               ("source_created_at", "observed_at"),
                               ("effective_at", "valid_until")):
            if value.get(earlier) and value.get(later):
                if datetime.fromisoformat(value[earlier]) > datetime.fromisoformat(value[later]):
                    raise ContractError("invalid timestamp ordering")


@dataclass(frozen=True)
class IntakeEnvelope(Record):
    kind = "intake"
    id_field = "intake_id"


@dataclass(frozen=True)
class EvidenceReference(Record):
    kind = "evidence-reference"
    id_field = "evidence_id"


@dataclass(frozen=True)
class Proposal(Record):
    kind = "proposal"
    id_field = "proposal_id"

    def _semantic(self, value):
        super()._semantic(value)
        if digest(value["assertion"].encode("utf-8")) != value["content_sha256"]:
            raise ContractError("assertion digest mismatch")


@dataclass(frozen=True)
class KnowledgeRevision(Record):
    kind = "knowledge-revision"
    id_field = "revision_id"


@dataclass(frozen=True)
class OutcomeContract(Record):
    kind = "outcome-contract"
    id_field = "contract_id"

    def _semantic(self, value):
        super()._semantic(value)
        ids = [c["criterion_id"] for c in value["criteria"]]
        if len(set(ids)) != len(ids):
            raise ContractError("duplicate criterion")
        body = {k: v for k, v in value.items() if k != "digest"}
        if digest(canonical(body).encode("utf-8")) != value["digest"]:
            raise ContractError("contract digest mismatch")


@dataclass(frozen=True)
class VerificationReport(Record):
    kind = "verification-report"
    id_field = "report_id"

    def _semantic(self, value):
        super()._semantic(value)
        ids = [c["criterion_id"] for c in value["checks"]]
        if len(set(ids)) != len(ids):
            raise ContractError("duplicate check")

    def validate_against(self, contract: OutcomeContract,
                         evidence: dict[str, EvidenceReference]) -> None:
        report, frozen = self.data, contract.data
        project_id = report["project_id"]
        for field in ("project_id", "task_id", "contract_id"):
            if report[field] != frozen[field]:
                raise ContractError("report contract binding mismatch")
        if report["contract_digest"] != frozen["digest"]:
            raise ContractError("report digest binding mismatch")
        checks = {c["criterion_id"]: c for c in report["checks"]}
        if set(checks) != {c["criterion_id"] for c in frozen["criteria"]}:
            raise ContractError("missing or extra checks")
        required = []
        for criterion in frozen["criteria"]:
            check = checks[criterion["criterion_id"]]
            for key in ("verifier_id", "verifier_version"):
                if check[key] != criterion[key]:
                    raise ContractError("verifier binding mismatch")
            for eid in check["evidence_ids"]:
                item = evidence.get(eid)
                if item is None:
                    raise ContractError("missing scoped evidence")
                data = item.data
                if (data["evidence_id"] != eid or
                        data["project_id"] != project_id or
                        data["attempt_id"] != report["attempt_id"] or
                        data["integrity"] != "HASH_VERIFIED" or
                        datetime.fromisoformat(data["observed_at"]) >
                        datetime.fromisoformat(report["verified_at"])):
                    raise ContractError("evidence attempt/integrity/time mismatch")
            if criterion["required"]:
                required.append(check["status"])
        expected = "FAIL" if "FAIL" in required else "UNKNOWN" if "UNKNOWN" in required else "PASS"
        if report["status"] != expected:
            raise ContractError("aggregate status mismatch")


@dataclass(frozen=True)
class MemoryMigrationManifest(Record):
    kind = "memory-migration-manifest"
    id_field = "migration_id"

    def _semantic(self, value):
        super()._semantic(value)
        steps = ["SNAPSHOT", "TARGET_CREATED", "OBSERVATIONS_COPIED",
                 "RELATIONS_COPIED", "ALIAS_LINKED", "READBACK_VERIFIED"]
        done = value["completed_steps"]
        if done != steps[:len(done)]:
            raise ContractError("migration steps out of order")
        if value["status"] == "VERIFIED" and done != steps:
            raise ContractError("migration verification incomplete")


RECORD_TYPES = {cls.kind: cls for cls in (IntakeEnvelope, EvidenceReference,
    Proposal, KnowledgeRevision, OutcomeContract, VerificationReport, MemoryMigrationManifest)}
