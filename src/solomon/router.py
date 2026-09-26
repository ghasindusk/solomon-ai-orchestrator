"""Router (Architecture doc section 2 "Router" / FR-05 / FR-06 / Phase 7
Learning Router).

Scores candidate adapters for a Task's Role using a weighted formula
based on the architecture doc's example (0.30 SkillMatch + 0.20
HistoricalQuality + 0.10 ProjectExperience + 0.10 ContextFitness + 0.10
Availability + 0.05 PrivacyFitness + 0.05 Speed + 0.10 UsageEfficiency),
extended in Phase 7 with a role_fitness component (weight taken from
HistoricalQuality to keep the total at 1.0 -- see
04_Config_Schemas/routing_weights.yaml for the exact current split).

Every component is sourced from something real: adapter capability config,
a live health() check, or recorded Task Result history in the State Store
-- never fabricated. Where Solomon genuinely has no data yet
(usage_efficiency, pending the Phase 4 Usage Manager; any history-based
component with too few samples, including role_fitness below
_MIN_SAMPLES_FOR_ROLE_FITNESS), the component is a documented neutral
placeholder (0.5) and a note is attached, so callers can tell "measured"
from "no data yet" apart. `StateStore.get_role_performance` exposes the
same underlying data as a raw per-role/per-agent table for inspection
(`solomon.cli learning-report`), so the calibration stays auditable
instead of being a hidden black box.

Roles are provider-independent (FR-05); ROLE_CAPABILITY_MAP is a
deliberately simple first-pass mapping from Solomon's role vocabulary
(spec FR-05) to the capability tags in 04_Config_Schemas/agents.example.yaml.

Octavryn SI v0.5 (roadmap R2/R3, spec 01 "Capability-first routing"):
candidates are resolved as Task -> Skill -> required capabilities ->
Capability Graph -> candidates, *before* any provider is scored. By
default the role picks the core skill `role.<role>` (skill_packs/core),
which requires the same capability ROLE_CAPABILITY_MAP names, so v0.4
routing results are unchanged. ROLE_CAPABILITY_MAP is kept as the
fallback when no skill registry is available. The graph gets DECLARED
evidence from agents.yaml. Callers with an IntelligenceRegistry can pass
a richer graph (historically verified / user-confirmed evidence), which
feeds the `capability_evidence` score component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .adapters.base import AdapterHealth
from .capability_graph import CapabilityGraph
from .descriptors import (
    EVIDENCE_RANK,
    Availability,
    CapabilityEvidence,
    EvidenceState,
    ExecutionCandidate,
    IntelligenceDescriptor,
    Locality,
    SkillDescriptor,
)
from .models import Task
from .state import StateStore
from .token_budget import TokenBudgetManager
from .usage import UsageManager

_AGENTS_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "agents.example.yaml"
)
_WEIGHTS_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "routing_weights.yaml"
)

ROLE_CAPABILITY_MAP = {
    "architect": "architecture",
    "coder": "coding",
    "reviewer": "review",
    "tester": "testing",
    "researcher": "analysis",
    "documenter": "documentation",
    "environment_operator": "environment_operations",
    "knowledge_curator": "rag",
    "planner": "architecture",
}

NEUTRAL = 0.5
# Phase 7: don't trust a role-specific success rate until there's enough
# signal to distinguish "genuinely weak at this role" from noise.
_MIN_SAMPLES_FOR_ROLE_FITNESS = 3


@dataclass
class AgentScore:
    adapter_name: str
    total: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Router:
    def __init__(
        self,
        agents_config_path: Path | str | None = None,
        weights_path: Path | str | None = None,
        state: StateStore | None = None,
        usage_manager: UsageManager | None = None,
        token_budget_manager: TokenBudgetManager | None = None,
        skill_registry=None,
        capability_graph: CapabilityGraph | None = None,
        project_repo_path: str | None = None,
    ):
        agents_path = Path(agents_config_path) if agents_config_path else _AGENTS_CONFIG_PATH
        weights_path_ = Path(weights_path) if weights_path else _WEIGHTS_CONFIG_PATH
        with open(agents_path, "r", encoding="utf-8") as f:
            self._agents_config = (yaml.safe_load(f) or {}).get("agents", {})
        with open(weights_path_, "r", encoding="utf-8") as f:
            weights_raw = yaml.safe_load(f) or {}
        self._weights: dict[str, float] = weights_raw.get("weights", {})
        self._adapter_props: dict[str, dict] = weights_raw.get("adapters", {})
        self._default_context_window = weights_raw.get("default_context_window_tokens", 32768)
        self._state = state
        # D27 step 7: usage_manager (cost_usd/USD) is now a fallback, not a
        # required Core input -- token_budget_manager (Token-counted) is
        # the primary usage_efficiency signal when both state and a
        # TokenBudgetManager are available. usage_manager stays supported
        # (not removed) for callers that haven't set up a Token Budget
        # config yet; long-term, cost_usd belongs to a Cost Calculator
        # addon outside Core, not Router.
        self._usage_manager = usage_manager
        self._token_budget_manager = token_budget_manager
        self._gpu_busy_threshold = weights_raw.get("gpu_awareness", {}).get("busy_threshold_percent", 70)
        self._graph = capability_graph or self._graph_from_config()
        if skill_registry is None:
            from .skills import SkillRegistry

            # D66: project-scope skills (<repo>/.octavryn/skills) are loaded
            # when the caller routes for a project, so an explicit project
            # skill resolves at execution time exactly as governance saw it.
            skill_registry = SkillRegistry.default(
                graph=self._graph, include_user=False, project_repo_path=project_repo_path
            )
        self._skills = skill_registry
        self.last_resolution: list[ExecutionCandidate] = []

    def _graph_from_config(self) -> CapabilityGraph:
        """DECLARED-only graph built from agents.yaml. Availability is
        UNKNOWN here because the live health check is scored separately
        (the `availability` component), exactly as in v0.4."""
        try:
            graph = CapabilityGraph.load()
        except FileNotFoundError:
            graph = CapabilityGraph({})
        for name, cfg in self._agents_config.items():
            graph.add_intelligence(
                IntelligenceDescriptor(
                    id=name,
                    display_name=name,
                    adapter_type=str(cfg.get("type", "unknown")),
                    locality=Locality.LOCAL if cfg.get("type") == "local" else Locality.UNKNOWN,
                    capabilities=[
                        CapabilityEvidence(c, EvidenceState.DECLARED, "config:agents.yaml")
                        for c in (cfg.get("primary_capabilities") or [])
                    ],
                )
            )
        return graph

    @property
    def skill_registry(self):
        return self._skills

    @property
    def capability_graph(self) -> CapabilityGraph:
        return self._graph

    def skill_for_task(self, task: Task, skill_id: str | None = None) -> SkillDescriptor | None:
        if self._skills is None:
            return None
        if skill_id:
            return self._skills.get(skill_id)
        return self._skills.for_role(task.role)

    def _required(self, task: Task, skill: SkillDescriptor | None) -> list[str]:
        if skill is not None:
            return [self._graph.normalize(c) for c in skill.required_capabilities]
        capability = ROLE_CAPABILITY_MAP.get(task.role)
        return [capability] if capability else []

    def resolve_candidates(self, task: Task, skill: SkillDescriptor | None = None) -> list[ExecutionCandidate]:
        """Capability-first candidate resolution. Returns every known
        intelligence as an ExecutionCandidate, with the missing
        capabilities or exclusion reason filled in, so callers can explain
        why something was *not* chosen. Eligible ones have `.eligible`."""
        if skill is None:
            skill = self.skill_for_task(task)
        required = self._required(task, skill)
        if not required:
            self.last_resolution = []
            return []
        out: list[ExecutionCandidate] = []
        for desc in sorted(self._graph.intelligences(), key=lambda d: d.id):
            evidence = self._graph.evidence_for(desc.id)
            cand = ExecutionCandidate(intelligence_id=desc.id, skill_id=skill.id if skill else None)
            for cap in required:
                if cap in evidence:
                    cand.satisfied[cap] = evidence[cap].value
                else:
                    cand.missing.append(cap)
            if desc.availability == Availability.ABSENT:
                cand.excluded_reason = "absent (no longer discovered)"
            elif skill is not None and skill.locality_constraint == "local" and desc.locality != Locality.LOCAL:
                cand.excluded_reason = "skill requires local execution"
            elif skill is not None and skill.locality_constraint == "cloud" and desc.locality == Locality.LOCAL:
                cand.excluded_reason = "skill requires cloud execution"
            elif skill is not None and skill.provider_constraints and desc.provider not in skill.provider_constraints:
                cand.excluded_reason = f"provider '{desc.provider}' not in skill provider_constraints"
            out.append(cand)
        self.last_resolution = out
        return out

    def candidates_for_role(self, role: str, skill: SkillDescriptor | None = None) -> list[str]:
        probe = Task(goal_id="-", project_id="-", type="-", role=role, definition_of_done=[])
        return [c.intelligence_id for c in self.resolve_candidates(probe, skill=skill) if c.eligible]

    def _evidence_component(self, adapter_name: str, task: Task, skill: SkillDescriptor | None) -> tuple[float, str]:
        required = self._required(task, skill)
        if not required:
            return 0.0, "capability_evidence: no required capabilities resolved"
        evidence = self._graph.evidence_for(adapter_name)
        weakest = min((evidence.get(c, EvidenceState.UNKNOWN) for c in required), key=lambda s: EVIDENCE_RANK[s])
        return EVIDENCE_RANK[weakest] / 4.0, f"capability_evidence: weakest evidence '{weakest.value}'"

    def score_adapter(
        self,
        adapter_name: str,
        task: Task,
        health: AdapterHealth | None = None,
        gpu_telemetry: dict | None = None,
        token_pressure: float | None = None,
        skill: SkillDescriptor | None = None,
    ) -> AgentScore:
        components: dict[str, float] = {}
        notes: list[str] = []

        # v0.5: skill_match = the capability graph says this adapter has
        # every capability the task's skill requires. For core role skills
        # this is the same answer as v0.4's role -> capability lookup.
        if skill is None:
            skill = self.skill_for_task(task)
        required = self._required(task, skill)
        evidence = self._graph.evidence_for(adapter_name)
        components["skill_match"] = 1.0 if required and all(c in evidence for c in required) else 0.0
        components["capability_evidence"], ev_note = self._evidence_component(adapter_name, task, skill)
        notes.append(ev_note)

        if health is not None:
            components["availability"] = 1.0 if health.available else 0.0
        else:
            components["availability"] = 0.0
            notes.append("availability: no health check performed, scored 0.0")

        props = self._adapter_props.get(adapter_name, {})
        components["privacy_fitness"] = float(props.get("privacy_fitness", NEUTRAL))

        window = props.get("context_window_tokens", self._default_context_window)
        if task.context_budget_tokens:
            components["context_fitness"] = max(0.0, min(1.0, window / task.context_budget_tokens))
        else:
            components["context_fitness"] = 1.0

        if self._state is not None:
            project_stats = self._state.get_adapter_stats(adapter_name, project_id=task.project_id)
            global_stats = self._state.get_adapter_stats(adapter_name)
            role_stats = self._state.get_adapter_stats(adapter_name, role=task.role)
        else:
            project_stats = {"count": 0, "success_rate": None, "avg_duration_seconds": None}
            global_stats = project_stats
            role_stats = project_stats
            notes.append("no StateStore provided: historical/experience/speed/role_fitness all neutral")

        if global_stats["success_rate"] is not None:
            components["historical_quality"] = global_stats["success_rate"]
        else:
            components["historical_quality"] = NEUTRAL
            notes.append("historical_quality: no recorded task history yet, neutral default")

        experience_count = project_stats["count"]
        components["project_experience"] = min(1.0, experience_count / 5.0)
        if experience_count == 0:
            notes.append("project_experience: no prior recorded tasks for this project")

        if role_stats["count"] >= _MIN_SAMPLES_FOR_ROLE_FITNESS and role_stats["success_rate"] is not None:
            components["role_fitness"] = role_stats["success_rate"]
        else:
            components["role_fitness"] = NEUTRAL
            notes.append(
                f"role_fitness: only {role_stats['count']} recorded sample(s) for role "
                f"'{task.role}' (need {_MIN_SAMPLES_FOR_ROLE_FITNESS}+), neutral default"
            )

        ref_duration = props.get("reference_duration_seconds", 30)
        avg_duration = global_stats["avg_duration_seconds"]
        if avg_duration is not None and avg_duration > 0:
            components["speed"] = max(0.0, min(1.0, ref_duration / avg_duration))
        else:
            components["speed"] = NEUTRAL
            notes.append("speed: no recorded durations yet, neutral default")

        is_local_free = components["privacy_fitness"] >= 1.0

        # D27 step 7: Token-based pressure (pre-computed by route() from
        # TokenBudgetManager, see there) is now the primary usage_efficiency
        # signal. The USD-based UsageManager.pressure() is a fallback for
        # callers that haven't configured a Token Budget yet -- no longer
        # required for Core routing to produce a real (non-neutral) signal.
        if token_pressure is not None:
            if is_local_free:
                components["usage_efficiency"] = 1.0
            else:
                components["usage_efficiency"] = max(0.0, 1.0 - token_pressure)
                if token_pressure > 0:
                    notes.append(f"usage_efficiency: Token budget pressure {token_pressure:.0%}")
        elif self._usage_manager is not None:
            if is_local_free:
                components["usage_efficiency"] = 1.0
            else:
                pressure = self._usage_manager.pressure(task.project_id)
                components["usage_efficiency"] = max(0.0, 1.0 - pressure)
                if pressure > 0:
                    notes.append(f"usage_efficiency: legacy USD budget pressure {pressure:.0%} (no Token Budget configured)")
        else:
            components["usage_efficiency"] = NEUTRAL
            notes.append("usage_efficiency: no TokenBudgetManager or UsageManager provided, neutral placeholder")

        # GPU-aware routing: a local/GPU-bound adapter competes with
        # whatever else is using the GPU right now (e.g. the user playing
        # Minecraft) -- only penalizes local adapters, never cloud ones,
        # and only when telemetry is actually supplied (absence of data
        # is not "the GPU is busy"). Real, live nvidia-smi data
        # (telemetry.get_gpu_telemetry), never fabricated.
        if is_local_free and gpu_telemetry is not None:
            gpu_load = gpu_telemetry.get("gpu_load_percent")
            if gpu_load is not None and gpu_load >= self._gpu_busy_threshold:
                components["usage_efficiency"] = min(
                    components["usage_efficiency"], max(0.0, 1.0 - gpu_load / 100)
                )
                notes.append(
                    f"usage_efficiency: GPU busy ({gpu_load:.0f}% load >= "
                    f"{self._gpu_busy_threshold}% threshold), local adapter penalized"
                )

        total = sum(self._weights.get(name, 0.0) * value for name, value in components.items())
        return AgentScore(adapter_name=adapter_name, total=total, components=components, notes=notes)

    def route(
        self,
        task: Task,
        health_checks: dict[str, AdapterHealth] | None = None,
        gpu_telemetry: dict | None = None,
        skill: SkillDescriptor | None = None,
    ) -> list[AgentScore]:
        health_checks = health_checks or {}
        # candidates_for_role stays the single candidate seam (tests and
        # callers override it); an explicit skill is forwarded to it.
        if skill is None:
            candidates = self.candidates_for_role(task.role)
            skill = self.skill_for_task(task)
        else:
            candidates = self.candidates_for_role(task.role, skill=skill)

        # D27 step 7: computed once per route() call (not per candidate --
        # every candidate shares the same project-scoped Token Budget
        # status) from the project's UsageRecords, same pattern as
        # gpu_telemetry being a single live snapshot shared across
        # candidates.
        token_pressure: float | None = None
        if self._token_budget_manager is not None and self._state is not None:
            records = self._state.get_usage_records(project_id=task.project_id)
            status = self._token_budget_manager.check(records, scope="project", scope_id=task.project_id)
            if status.pct_used is not None:
                token_pressure = max(0.0, min(1.0, status.pct_used / 100))

        scores = [
            self.score_adapter(
                name, task, health=health_checks.get(name), gpu_telemetry=gpu_telemetry,
                token_pressure=token_pressure, skill=skill,
            )
            for name in candidates
        ]
        scores.sort(key=lambda s: s.total, reverse=True)
        return scores
