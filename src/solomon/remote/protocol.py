"""Remote Task Protocol (Octavryn SI v0.5 remote extension, spec 18).

Pure data + validation. No transport, no I/O. Validation is strict and
fails closed: an unknown enum value, a missing field, a non-UUID id, or a
lifetime above MAX_TTL makes the whole envelope invalid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

PROTOCOL_VERSION = "0.5"
MAX_TTL = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=2)

_DEVICE_CLASSES = {"mobile", "desktop", "web", "unknown"}
_AUTONOMY = {"supervised", "bounded", "delegated"}
_POLICY = {"deny", "approval_required"}
_LOCALITY = {"any", "local"}


class RemoteStatus(str, Enum):
    QUEUED = "queued"
    OFFLINE = "offline"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"  # an adapter produced a result
    VERIFIED = "verified"    # Definition of Done passed (task COMPLETE)
    REJECTED = "rejected"    # never accepted (auth/replay/expiry/scope/validation)


class ProtocolError(ValueError):
    pass


def _parse_ts(value, name: str) -> datetime:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ProtocolError(f"{name}: not an ISO-8601 timestamp")
    if ts.tzinfo is None:
        raise ProtocolError(f"{name}: timezone required")
    return ts


def _uuid(value, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        raise ProtocolError(f"{name}: not a UUID")


@dataclass
class Constraints:
    autonomy: str = "supervised"
    external_publication: str = "deny"
    destructive_actions: str = "deny"
    locality: str = "any"


@dataclass
class TaskEnvelope:
    request_id: str
    correlation_id: str
    actor_identity_ref: str
    source_surface: str
    device_class: str
    project_id: str
    goal: str
    created_at: datetime
    expires_at: datetime
    role: str = "coder"
    continuation_task_id: str | None = None
    continuation_checkpoint_id: str | None = None
    constraints: Constraints = field(default_factory=Constraints)
    schema_version: str = PROTOCOL_VERSION

    @classmethod
    def parse(cls, data: dict) -> "TaskEnvelope":
        if not isinstance(data, dict):
            raise ProtocolError("envelope must be an object")
        if str(data.get("schema_version")) != PROTOCOL_VERSION:
            raise ProtocolError(f"schema_version must be {PROTOCOL_VERSION}")
        actor = data.get("actor") or {}
        if actor.get("type") != "human":
            raise ProtocolError("actor.type must be 'human'")
        if not actor.get("identity_ref"):
            raise ProtocolError("actor.identity_ref required")
        source = data.get("source") or {}
        device = source.get("device_class", "unknown")
        if device not in _DEVICE_CLASSES:
            raise ProtocolError(f"source.device_class invalid: {device!r}")
        c = data.get("constraints") or {}
        constraints = Constraints(
            autonomy=c.get("autonomy", "supervised"),
            external_publication=c.get("external_publication", "deny"),
            destructive_actions=c.get("destructive_actions", "deny"),
            locality=c.get("locality", "any"),
        )
        if constraints.autonomy not in _AUTONOMY:
            raise ProtocolError("constraints.autonomy invalid")
        if constraints.external_publication not in _POLICY or constraints.destructive_actions not in _POLICY:
            raise ProtocolError("constraints publication/destructive policy invalid")
        if constraints.locality not in _LOCALITY:
            raise ProtocolError("constraints.locality invalid")
        goal = str(data.get("goal") or "").strip()
        project_id = str(data.get("project_id") or "").strip()
        if not goal:
            raise ProtocolError("goal required")
        if not project_id:
            raise ProtocolError("project_id required")
        created = _parse_ts(data.get("created_at"), "created_at")
        expires = _parse_ts(data.get("expires_at"), "expires_at")
        if expires <= created:
            raise ProtocolError("expires_at must be after created_at")
        if expires - created > MAX_TTL:
            raise ProtocolError(f"lifetime exceeds {MAX_TTL}")
        cont = data.get("continuation") or {}
        return cls(
            request_id=_uuid(data.get("request_id"), "request_id"),
            correlation_id=_uuid(data.get("correlation_id"), "correlation_id"),
            actor_identity_ref=str(actor["identity_ref"]),
            source_surface=str(source.get("surface", "unknown")),
            device_class=device,
            project_id=project_id,
            goal=goal,
            created_at=created,
            expires_at=expires,
            role=str(data.get("role") or "coder"),
            continuation_task_id=cont.get("task_id"),
            continuation_checkpoint_id=cont.get("checkpoint_id"),
            constraints=constraints,
        )

    def is_live(self, now: datetime) -> bool:
        return self.created_at - CLOCK_SKEW <= now <= self.expires_at


def new_envelope(identity_ref: str, project_id: str, goal: str, *, surface: str = "chatgpt",
                 device_class: str = "mobile", ttl: timedelta = timedelta(minutes=10),
                 constraints: dict | None = None, role: str = "coder",
                 continuation: dict | None = None, now: datetime | None = None) -> dict:
    """Convenience builder (client side / tests)."""
    now = now or datetime.now(timezone.utc)
    return {
        "schema_version": PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "actor": {"type": "human", "identity_ref": identity_ref},
        "source": {"surface": surface, "device_class": device_class},
        "project_id": project_id,
        "role": role,
        "continuation": continuation or {},
        "goal": goal,
        "constraints": constraints or {},
        "created_at": now.isoformat(),
        "expires_at": (now + ttl).isoformat(),
    }
