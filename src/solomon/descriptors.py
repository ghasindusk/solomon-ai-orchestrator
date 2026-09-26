"""Octavryn SI v0.5 core schemas (spec 01 "Core entities", roadmap R1).

Provider-independent descriptors for every kind of participant Core
reasons about: intelligences (AI adapters, local models), capabilities,
skills, tools, human participants and interaction surfaces, plus the
routing/verification records built from them.

Design rules carried from the spec:
- Core never needs a *named* provider -- nothing here enumerates
  providers; `provider` is free text supplied by an integration.
- UNKNOWN is a real state, never coerced to success/zero
  (EvidenceState.UNKNOWN, Availability.UNKNOWN, `None` numbers).
- Declaring or discovering something grants no permission:
  `granted_permissions` is always computed by governance, never copied
  from a descriptor's `requested_permissions`.

Every descriptor round-trips through to_dict()/from_dict() and carries
`schema_version`. from_dict() rejects a different *major* schema version
instead of guessing at an unknown shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum

SCHEMA_VERSION = "0.5"


class SchemaVersionError(ValueError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_schema_version(version: str | None) -> str:
    """Accept any 0.5.x; reject missing or other versions. 0.x minors are
    treated as breaking (pre-1.0 semver), so 0.4 data must go through an
    explicit migration instead of being read as if it were 0.5."""
    if not version:
        raise SchemaVersionError("schema_version is required")
    parts = str(version).split(".")
    expected = SCHEMA_VERSION.split(".")
    if parts[:2] != expected[:2]:
        raise SchemaVersionError(f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION}.x)")
    return str(version)


class EvidenceState(str, Enum):
    """How much we actually know that an intelligence has a capability
    (spec 03). Ordered weakest -> strongest by EVIDENCE_RANK."""

    UNKNOWN = "unknown"
    DECLARED = "declared"
    PROBED = "probed"
    HISTORICALLY_VERIFIED = "historically_verified"
    USER_CONFIRMED = "user_confirmed"


EVIDENCE_RANK = {
    EvidenceState.UNKNOWN: 0,
    EvidenceState.DECLARED: 1,
    EvidenceState.PROBED: 2,
    EvidenceState.HISTORICALLY_VERIFIED: 3,
    EvidenceState.USER_CONFIRMED: 4,
}


class Availability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ABSENT = "absent"  # was registered before, not found by the latest discovery
    UNKNOWN = "unknown"


class Locality(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class SkillScope(str, Enum):
    """Spec 04 scopes. SCOPE_PRECEDENCE: higher wins on an id conflict
    across scopes. Project beats user (a project can pin its own variant),
    user beats addon/MCP/provider (the human's own config beats code
    someone else shipped), and everything beats core defaults."""

    CORE = "core"
    PROVIDER = "provider"
    MCP = "mcp"
    ADDON = "addon"
    USER = "user"
    PROJECT = "project"


SCOPE_PRECEDENCE = {
    SkillScope.CORE: 0,
    SkillScope.PROVIDER: 1,
    SkillScope.MCP: 2,
    SkillScope.ADDON: 3,
    SkillScope.USER: 4,
    SkillScope.PROJECT: 5,
}


@dataclass
class CapabilityEvidence:
    capability: str
    state: EvidenceState = EvidenceState.UNKNOWN
    source: str = ""
    observed_at: str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "state": self.state.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityEvidence":
        return cls(
            capability=data["capability"],
            state=EvidenceState(data.get("state", "unknown")),
            source=data.get("source", ""),
            observed_at=data.get("observed_at"),
            detail=data.get("detail", ""),
        )


@dataclass
class CapabilityDescriptor:
    id: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    implies: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityDescriptor":
        check_schema_version(data.get("schema_version", SCHEMA_VERSION))
        return cls(
            id=data["id"],
            description=data.get("description", ""),
            aliases=list(data.get("aliases") or []),
            implies=list(data.get("implies") or []),
        )


@dataclass
class IntelligenceDescriptor:
    id: str
    display_name: str
    adapter_type: str  # cli | http | desktop_surface | mcp | human | ...
    provider: str = "unknown"
    locality: Locality = Locality.UNKNOWN
    availability: Availability = Availability.UNKNOWN
    capabilities: list[CapabilityEvidence] = field(default_factory=list)
    modalities: list[str] = field(default_factory=lambda: ["text"])
    requested_permissions: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    health_detail: str = ""
    discovered_at: str | None = None
    verified_at: str | None = None
    provenance: str = ""
    schema_version: str = SCHEMA_VERSION

    def capability_states(self) -> dict[str, EvidenceState]:
        """Strongest evidence per capability (several sources may report
        the same capability)."""
        best: dict[str, EvidenceState] = {}
        for ev in self.capabilities:
            cur = best.get(ev.capability)
            if cur is None or EVIDENCE_RANK[ev.state] > EVIDENCE_RANK[cur]:
                best[ev.capability] = ev.state
        return best

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "adapter_type": self.adapter_type,
            "provider": self.provider,
            "locality": self.locality.value,
            "availability": self.availability.value,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "modalities": list(self.modalities),
            "requested_permissions": list(self.requested_permissions),
            "models": list(self.models),
            "health_detail": self.health_detail,
            "discovered_at": self.discovered_at,
            "verified_at": self.verified_at,
            "provenance": self.provenance,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IntelligenceDescriptor":
        check_schema_version(data.get("schema_version"))
        return cls(
            id=data["id"],
            display_name=data.get("display_name", data["id"]),
            adapter_type=data.get("adapter_type", "unknown"),
            provider=data.get("provider", "unknown"),
            locality=Locality(data.get("locality", "unknown")),
            availability=Availability(data.get("availability", "unknown")),
            capabilities=[CapabilityEvidence.from_dict(c) for c in data.get("capabilities") or []],
            modalities=list(data.get("modalities") or ["text"]),
            requested_permissions=list(data.get("requested_permissions") or []),
            models=list(data.get("models") or []),
            health_detail=data.get("health_detail", ""),
            discovered_at=data.get("discovered_at"),
            verified_at=data.get("verified_at"),
            provenance=data.get("provenance", ""),
            schema_version=data["schema_version"],
        )


@dataclass
class SkillDescriptor:
    id: str
    version: int
    name: str
    description: str = ""
    required_capabilities: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=lambda: ["text"])
    risk: str = "NORMAL"
    requested_permissions: list[str] = field(default_factory=list)
    provider_constraints: list[str] = field(default_factory=list)
    locality_constraint: str | None = None  # "local" => only local intelligences
    verification: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    provenance: str = ""
    enabled: bool = True
    scope: SkillScope = SkillScope.CORE
    roles: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("skill id is required")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"skill {self.id}: version must be a positive integer, got {self.version!r}")
        if self.risk not in ("LOW", "NORMAL", "HIGH", "VERY_HIGH", "CRITICAL"):
            raise ValueError(f"skill {self.id}: unknown risk {self.risk!r}")
        if self.locality_constraint not in (None, "local", "cloud"):
            raise ValueError(f"skill {self.id}: unknown locality_constraint {self.locality_constraint!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scope"] = self.scope.value
        return d

    @classmethod
    def from_dict(cls, data: dict, scope: SkillScope | None = None, provenance: str = "") -> "SkillDescriptor":
        check_schema_version(str(data.get("schema_version", SCHEMA_VERSION)))
        requires = data.get("requires") or {}
        return cls(
            id=data["id"],
            version=data.get("version", 0),
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            required_capabilities=list(requires.get("capabilities") or data.get("required_capabilities") or []),
            optional_capabilities=list(data.get("optional_capabilities") or []),
            tools=list(requires.get("tools") or data.get("tools") or []),
            context=list(data.get("context") or []),
            modalities=list(data.get("modalities") or ["text"]),
            risk=str(data.get("risk", "NORMAL")).upper(),
            requested_permissions=list(data.get("permissions") or data.get("requested_permissions") or []),
            provider_constraints=list(data.get("provider_constraints") or []),
            locality_constraint=data.get("locality_constraint"),
            verification=list(data.get("verification") or []),
            fallbacks=list(data.get("fallbacks") or []),
            provenance=provenance or data.get("provenance", ""),
            enabled=bool(data.get("enabled", True)),
            scope=scope or SkillScope(data.get("scope", "core")),
            roles=list(data.get("roles") or []),
        )


@dataclass
class ToolDescriptor:
    id: str
    kind: str  # cli | mcp | python | http
    available: Availability = Availability.UNKNOWN
    path: str | None = None
    requested_permissions: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        d = asdict(self)
        d["available"] = self.available.value
        return d


@dataclass
class HumanParticipant:
    """Spec 07: humans are first-class participants, but human authority
    is not interchangeable with model authority -- `can_approve` exists
    only on this type, never on IntelligenceDescriptor."""

    id: str
    display_name: str = "human"
    capabilities: list[str] = field(
        default_factory=lambda: [
            "approval", "judgment", "goal_definition", "ambiguity_resolution",
            "credential_entry", "physical_world_action", "publication_authorization",
        ]
    )
    can_approve: bool = True
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SurfaceDescriptor:
    """Spec 06: a desktop/chat app is an interaction surface, not an
    execution agent. `privileged_execution` is fixed False -- a surface
    only reaches execution through the same gated path as the CLI."""

    id: str
    type: str  # desktop_app | web | mobile | cli
    availability: Availability = Availability.UNKNOWN
    interaction_modes: list[str] = field(default_factory=lambda: ["conversation"])
    bridge: str | None = None  # mcp | desktop_extension | connector | none
    permissions: list[str] = field(default_factory=list)
    human_presence_required: bool = True
    exposed_capabilities: list[str] = field(default_factory=list)
    octavryn_capabilities_offered: list[str] = field(default_factory=list)
    detail: str = ""
    schema_version: str = SCHEMA_VERSION

    @property
    def privileged_execution(self) -> bool:
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["availability"] = self.availability.value
        d["privileged_execution"] = False
        return d


@dataclass
class ExecutionCandidate:
    intelligence_id: str
    skill_id: str | None
    satisfied: dict[str, str] = field(default_factory=dict)  # capability -> evidence state
    missing: list[str] = field(default_factory=list)
    excluded_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return not self.missing and self.excluded_reason is None

    def weakest_evidence(self) -> EvidenceState:
        if not self.satisfied:
            return EvidenceState.UNKNOWN
        return min((EvidenceState(s) for s in self.satisfied.values()), key=lambda s: EVIDENCE_RANK[s])


@dataclass
class VerificationProfile:
    id: str
    checks: list[str] = field(default_factory=list)  # e.g. result_recorded, tests, build, review_of:*
    required: bool = True


@dataclass
class PolicyProfile:
    id: str
    max_autonomous_risk: str = "NORMAL"
    allowed_permissions: list[str] = field(default_factory=list)
    allow_cloud: bool = True


@dataclass
class EvaluationRecord:
    task_id: str
    intelligence_id: str
    skill_id: str | None
    outcome: str  # success | verified_success | failure | unknown
    retries: int = 0
    tokens: int | None = None
    latency_s: float | None = None
    participants: list[str] = field(default_factory=list)
    recorded_at: str = field(default_factory=_utcnow)
