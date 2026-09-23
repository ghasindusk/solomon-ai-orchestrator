"""Policy Engine (Architecture doc section 2, Policies doc section 8).

Loads 03_Policies/GLOBAL_POLICY.yaml at startup. This is a safety/global
policy load and must NOT depend on RAG retrieval (knowledge.safety_rules_must_not_depend_on_rag).
Project/task policy layering is deferred to a later phase; only the global
layer is implemented here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import Risk

_RISK_ORDER = [Risk.LOW, Risk.NORMAL, Risk.HIGH, Risk.VERY_HIGH, Risk.CRITICAL]

_DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "03_Policies" / "GLOBAL_POLICY.yaml"
)


class PolicyEngine:
    def __init__(self, policy_path: Path | str | None = None):
        self.path = Path(policy_path) if policy_path else _DEFAULT_POLICY_PATH
        if not self.path.exists():
            raise FileNotFoundError(f"Global policy not found: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            self._policy = yaml.safe_load(f)

    @property
    def raw(self) -> dict:
        return self._policy

    def requires_approval(self, risk: Risk, autonomy: int | None = None) -> bool:
        """Safety levels L3/L4 (broad deletion, privileged OS, external
        publish) require approval regardless of risk-only classification;
        this lean gate approximates that with the Task's declared risk.

        v0.4 08_UX "autonomy dial" (Task.autonomy, 0-5): per the README's
        core principle "Safety above autonomy: policy and approval gates
        cannot be overridden by agents/addons", the dial can only ADD
        caution below this floor, never remove it -- VERY_HIGH/CRITICAL
        always require approval regardless of autonomy. A low dial (0-2)
        can additionally require approval for risk tiers the base policy
        would otherwise let through; 3-5 (or None, i.e. not set) defer
        entirely to the base policy."""
        safety = self._policy.get("safety", {})
        if risk in (Risk.VERY_HIGH, Risk.CRITICAL):
            return True
        if bool(safety.get("destructive_bulk_change_requires_approval")) and risk == Risk.HIGH:
            return True
        if autonomy is None:
            return False
        # Lower dial = escalate MORE risk tiers. 0: everything (even LOW).
        # 1: NORMAL and above. 2: HIGH and above (already the base policy's
        # floor, so this is a no-op unless the policy flag is off). 3-5:
        # no extra escalation.
        extra_caution_floor = {0: Risk.LOW, 1: Risk.NORMAL, 2: Risk.HIGH}.get(autonomy)
        if extra_caution_floor is None:
            return False
        return _RISK_ORDER.index(risk) >= _RISK_ORDER.index(extra_caution_floor)

    def checkpoint_required_before_code_change(self) -> bool:
        return bool(self._policy.get("safety", {}).get("checkpoint_before_code_change", True))

    def agent_claim_is_sufficient(self) -> bool:
        return bool(self._policy.get("completion", {}).get("agent_claim_is_sufficient", False))

    def definition_of_done_required(self) -> bool:
        return bool(
            self._policy.get("completion", {}).get("require_applicable_definition_of_done", True)
        )

    def direct_ai_conversation_allowed(self) -> bool:
        return bool(self._policy.get("communication", {}).get("direct_ai_conversation", False))

    def global_knowledge_paths(self) -> list[str]:
        """v0.4 Phase 3 'Project/global knowledge scopes': paths outside
        any single project's knowledge_path that should be available to
        every project's context pack (e.g. docs/ai_rules/). Distinct from
        knowledge.isolate_projects, which is about project-to-project
        isolation, not this deliberate shared-rules exception."""
        return list(self._policy.get("knowledge", {}).get("global_knowledge_paths") or [])
