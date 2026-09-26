"""Intelligence Registry and read-only discovery (Octavryn SI v0.5 spec 03,
roadmap R2).

Builds IntelligenceDescriptors for every known adapter by merging
evidence from independent sources, weakest to strongest:

  DECLARED               adapter's own AdapterDeclaration + agents.yaml
  HISTORICALLY_VERIFIED  >= _MIN_VERIFIED Tasks in that role reached
                         COMPLETE (Definition of Done passed); RESULT_RECEIVED
                         alone does not count
  USER_CONFIRMED         a human ran `capability confirm`

PROBED is defined but no built-in adapter implements probe_capability().
A real probe means a billed model call, so none is faked.

Discovery is read-only: it calls health()/list_models() only, never
execute(), and it writes nothing but the registry rows. It grants no
permission (spec 03 "Discovery ... MUST NOT grant execution permission").

Historical identity: rows are never deleted. An intelligence that stops
being discovered is marked `absent`. task_results/usage rows keyed by its
id stay attributable, and it becomes routable again when it reappears.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .adapters.base import AdapterHealth, is_unsupported
from .adapters.registry import known_adapter_names, load_adapter
from .capability_graph import CapabilityGraph
from .descriptors import (
    Availability,
    CapabilityEvidence,
    EvidenceState,
    IntelligenceDescriptor,
    Locality,
    ToolDescriptor,
)
from .state import StateStore

_AGENTS_CONFIG_PATH = Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "agents.example.yaml"
_MIN_VERIFIED = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _role_capability_map() -> dict[str, str]:
    from .router import ROLE_CAPABILITY_MAP

    return ROLE_CAPABILITY_MAP


class IntelligenceRegistry:
    def __init__(
        self,
        state: StateStore | None = None,
        graph: CapabilityGraph | None = None,
        agents_config_path: Path | str | None = None,
    ):
        self.state = state
        self.graph = graph or CapabilityGraph.load()
        path = Path(agents_config_path) if agents_config_path else _AGENTS_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._agents_config = (yaml.safe_load(f) or {}).get("agents", {}) or {}
        except FileNotFoundError:
            self._agents_config = {}

    # -- evidence sources ------------------------------------------------

    def _config_evidence(self, name: str) -> list[CapabilityEvidence]:
        caps = (self._agents_config.get(name) or {}).get("primary_capabilities") or []
        return [CapabilityEvidence(c, EvidenceState.DECLARED, "config:agents.yaml") for c in caps]

    def _historical_evidence(self, name: str) -> list[CapabilityEvidence]:
        if self.state is None:
            return []
        role_map = _role_capability_map()
        out = []
        for role, count in self.state.get_verified_completions(name).items():
            cap = role_map.get(role)
            if cap and count >= _MIN_VERIFIED:
                out.append(
                    CapabilityEvidence(
                        cap, EvidenceState.HISTORICALLY_VERIFIED, "state:task_results",
                        observed_at=_now(), detail=f"{count} COMPLETE task(s) as role '{role}'",
                    )
                )
        return out

    def _user_evidence(self, name: str) -> list[CapabilityEvidence]:
        if self.state is None:
            return []
        return [
            CapabilityEvidence(
                row["capability"], EvidenceState.USER_CONFIRMED, f"human:{row['confirmed_by']}",
                observed_at=row["confirmed_at"],
            )
            for row in self.state.list_capability_confirmations(name)
        ]

    # -- discovery -------------------------------------------------------

    def describe(self, name: str, health: AdapterHealth | None = None) -> IntelligenceDescriptor:
        """One adapter -> descriptor. Pass `health` to reuse a check the
        caller already made (health checks spawn CLIs and can take seconds)."""
        adapter = load_adapter(name)
        decl = adapter.describe()
        if health is None:
            health = adapter.health()
        if is_unsupported(decl):
            desc = IntelligenceDescriptor(
                id=name, display_name=name, adapter_type="unknown",
                provenance=f"adapter:{name}:no_declaration",
            )
        else:
            caps = adapter.describe_capabilities()
            desc = IntelligenceDescriptor(
                id=name,
                display_name=decl.display_name,
                adapter_type=decl.adapter_type,
                provider=decl.provider,
                locality=decl.locality,
                capabilities=[] if is_unsupported(caps) else list(caps),
                modalities=list(decl.modalities),
                requested_permissions=list(decl.requested_permissions),
                provenance=f"adapter:{name}:declaration",
            )
            if health.available:
                models = adapter.list_models()
                desc.models = [] if is_unsupported(models) else list(models)
        desc.availability = Availability.AVAILABLE if health.available else Availability.UNAVAILABLE
        desc.health_detail = (health.detail or "")[:500]
        desc.discovered_at = _now()
        desc.capabilities += self._config_evidence(name)
        hist = self._historical_evidence(name)
        user = self._user_evidence(name)
        desc.capabilities += hist + user
        if hist or user:
            desc.verified_at = _now()
        return desc

    def discover(
        self,
        names: list[str] | None = None,
        health_checks: dict[str, AdapterHealth] | None = None,
    ) -> list[IntelligenceDescriptor]:
        names = list(names) if names is not None else known_adapter_names()
        health_checks = health_checks or {}
        found = []
        for name in names:
            desc = self.describe(name, health=health_checks.get(name))
            found.append(desc)
            self.graph.add_intelligence(desc)
            if self.state is not None:
                self.state.upsert_intelligence(desc.id, desc.to_dict(), desc.availability.value)
        if self.state is not None:
            current = {d.id for d in found}
            for row in self.state.list_intelligences():
                if row["intelligence_id"] not in current and row["availability"] != "absent":
                    self.state.mark_intelligence_absent(row["intelligence_id"])
        return found

    def load_persisted(self) -> list[IntelligenceDescriptor]:
        """Rebuild the graph from stored rows without spawning any CLI.
        Absent rows are loaded with availability=ABSENT, so they show up in
        history but never in routing."""
        if self.state is None:
            return []
        out = []
        for row in self.state.list_intelligences():
            desc = IntelligenceDescriptor.from_dict(row["descriptor"])
            desc.availability = Availability(row["availability"])
            out.append(desc)
            self.graph.add_intelligence(desc)
        return out


# -- tool discovery (read-only) -----------------------------------------

DEFAULT_TOOL_NAMES = ["git", "python", "node", "npm", "java", "gradle", "flutter", "gh", "docker",
                      "claude", "codex", "agy", "ollama"]


def discover_cli_tools(names: list[str] | None = None) -> list[ToolDescriptor]:
    out = []
    for name in names or DEFAULT_TOOL_NAMES:
        path = shutil.which(name)
        out.append(
            ToolDescriptor(
                id=name, kind="cli",
                available=Availability.AVAILABLE if path else Availability.ABSENT,
                path=path,
            )
        )
    return out


def discover_mcp_servers(config_path: Path | str | None = None) -> list[ToolDescriptor]:
    """Names only, from Claude Code's user config. Server commands, args
    and env are deliberately not copied: env blocks can hold API keys,
    and the descriptor must not become a secret carrier. A missing or
    unreadable file returns [] (no MCP known, not an error).

    D69: Claude Code keeps user-scope servers at the top level and
    local-scope servers under projects[<path>].mcpServers; both are
    read (the local-scope ones are where `octavryn` is registered)."""
    p = Path(config_path) if config_path else Path(os.path.expanduser("~")) / ".claude.json"
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    names = set((data.get("mcpServers") or {}).keys())
    for project in (data.get("projects") or {}).values():
        if isinstance(project, dict):
            names |= set((project.get("mcpServers") or {}).keys())
    return [
        ToolDescriptor(id=f"mcp:{name}", kind="mcp", available=Availability.UNKNOWN)
        for name in sorted(names)
    ]


def discover_codex_mcp_servers(config_path: Path | str | None = None) -> list[ToolDescriptor]:
    """D69: names only, from the Codex CLI / ChatGPT Desktop config."""
    import tomllib

    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    p = Path(config_path) if config_path else Path(home) / "config.toml"
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return []
    return [
        ToolDescriptor(id=f"codex-mcp:{name}", kind="mcp", available=Availability.UNKNOWN)
        for name in sorted((data.get("mcp_servers") or {}).keys())
    ]


def locality_of(desc: IntelligenceDescriptor) -> Locality:
    return desc.locality
