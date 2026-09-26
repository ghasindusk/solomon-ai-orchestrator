"""Adapter Layer interface (Architecture doc section 2) and the Octavryn
SI v0.5 provider-independent adapter contract (spec 05, roadmap R4).

Every integration (localai_ollama, claude_code, codex, antigravity, and
any future one) implements `health` + `execute`. The rest of the v0.5
contract (`discover`, `list_models`, `describe_capabilities`,
`probe_capability`, `prepare_request`, `normalize_result`,
`normalize_usage`, `cleanup`) has a default here that returns an
explicit `Unsupported` value. An integration opts in by overriding it.
A default never pretends to support something (spec 13 #7 "never
fabricate unavailable provider/API support").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..descriptors import (
    Availability,
    CapabilityEvidence,
    EvidenceState,
    IntelligenceDescriptor,
    Locality,
)
from ..models import Task
from ..result import TaskResult


NO_BACKGROUND_SUFFIX = (
    "\n\nDo not start any background, detached, or run_in_background "
    "processes for this task. A non-interactive adapter invocation exits "
    "as soon as it reports its final result, which kills anything still "
    "running in the background and silently discards its work. Complete "
    "the task and report the final result synchronously within this "
    "single invocation."
)
"""Appended to every prompt sent to a CLI-based adapter that has its own
agentic tool-use loop (claude_code, codex). Added 2026-09-10 after a
claude_code invocation returned RESULT_RECEIVED with zero actual changes:
the raw response talked about "retrying" and "running in the background
now", strongly suggesting the child process backgrounded a long-running
step and then exited (as `-p` invocations do once they produce a result)
before that step finished -- see STATUS.md's "known issues" section for
the observed case. This is a blunt mitigation (a prompt suffix, not a
guarantee), not a fix for the underlying behavior, which is not yet
understood well enough to fix directly."""


class AdapterHealth:
    def __init__(self, available: bool, detail: str = ""):
        self.available = available
        self.detail = detail


@dataclass(frozen=True)
class Unsupported:
    """Explicit 'this integration does not implement that operation'.
    Distinct from failure (the operation exists but did not work) and from
    an empty result (the operation worked and found nothing)."""

    operation: str
    adapter: str
    reason: str = "not implemented by this integration"

    def __bool__(self) -> bool:  # an Unsupported is never truthy "success"
        return False


def is_unsupported(value: object) -> bool:
    return isinstance(value, Unsupported)


@dataclass
class AdapterDeclaration:
    """Static identity/contract declaration (spec 05). Everything here is
    *declared* by the integration's author, so capabilities built from it
    carry EvidenceState.DECLARED, never anything stronger."""

    name: str
    display_name: str
    adapter_type: str  # cli | http | ...
    provider: str
    locality: Locality
    contract_version: str = "0.5"
    execution_modes: list[str] = field(default_factory=lambda: ["non_interactive"])
    modalities: list[str] = field(default_factory=lambda: ["text"])
    capabilities: list[str] = field(default_factory=list)
    credentials: str = "none"  # none | provider_managed | api_key | ...
    requested_permissions: list[str] = field(default_factory=list)
    streaming: bool = False
    mcp_tool_support: bool = False
    telemetry: str = "none"  # none | tokens | tokens_and_cost


class AgentAdapter(ABC):
    name: str = "base"
    declaration: AdapterDeclaration | None = None

    @abstractmethod
    def health(self) -> AdapterHealth:
        """Cheap check that the underlying CLI/API is reachable/usable."""

    @abstractmethod
    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        """Run the task non-interactively and return a structured TaskResult.

        Implementations must not raise on ordinary command failure; a
        non-zero exit or provider error is reported via TaskResult.status
        and TaskResult.uncertainties/evidence, so the Director can still
        record evidence and decide next steps (spec section 6: agent
        output is evidence, not completion).
        """

    def cancel(self, task: Task) -> None:  # pragma: no cover - optional
        """Best-effort cancellation hook; adapters without one may no-op."""
        return None

    # --- v0.5 contract (spec 05). Defaults are explicit Unsupported. ---

    def health_check(self) -> AdapterHealth:
        return self.health()

    def describe(self) -> AdapterDeclaration | Unsupported:
        if self.declaration is None:
            return Unsupported("describe", self.name)
        return self.declaration

    def describe_capabilities(self) -> list[CapabilityEvidence] | Unsupported:
        decl = self.describe()
        if is_unsupported(decl):
            return decl
        return [
            CapabilityEvidence(capability=c, state=EvidenceState.DECLARED, source=f"adapter:{self.name}")
            for c in decl.capabilities
        ]

    def discover(self) -> IntelligenceDescriptor | Unsupported:
        """Read-only (spec 03): runs health() only, never execute(), and
        grants nothing. requested_permissions is copied from the declaration
        for display only. Governance decides what is actually granted."""
        decl = self.describe()
        if is_unsupported(decl):
            return Unsupported("discover", self.name, "no AdapterDeclaration")
        health = self.health()
        caps = self.describe_capabilities()
        models = self.list_models()
        return IntelligenceDescriptor(
            id=decl.name,
            display_name=decl.display_name,
            adapter_type=decl.adapter_type,
            provider=decl.provider,
            locality=decl.locality,
            availability=Availability.AVAILABLE if health.available else Availability.UNAVAILABLE,
            capabilities=[] if is_unsupported(caps) else caps,
            modalities=list(decl.modalities),
            requested_permissions=list(decl.requested_permissions),
            models=[] if is_unsupported(models) else list(models),
            health_detail=health.detail[:500],
            discovered_at=self._now(),
            provenance=f"adapter:{self.name}:discover",
        )

    def list_models(self) -> list[str] | Unsupported:
        return Unsupported("list_models", self.name)

    def probe_capability(self, capability: str) -> CapabilityEvidence | Unsupported:
        return Unsupported("probe_capability", self.name)

    def prepare_request(self, task: Task, prompt: str) -> dict | Unsupported:
        return Unsupported("prepare_request", self.name)

    def normalize_result(self, raw: object, task: Task) -> TaskResult | Unsupported:
        return Unsupported("normalize_result", self.name)

    def normalize_usage(self, raw: object) -> object | Unsupported:
        return Unsupported("normalize_usage", self.name)

    def cleanup(self) -> None | Unsupported:
        return Unsupported("cleanup", self.name)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
