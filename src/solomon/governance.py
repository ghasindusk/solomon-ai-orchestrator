"""Unified governance gates (Octavryn SI v0.5 spec 09, roadmap R6).

Before v0.5, only `run-task` ran the risk/approval/budget gates.
`route-and-run`, `run-batch`, `debate` and `review-task` reached an
adapter's execute() without them (the known v0.4 gap, spec 09). Every
execution path now calls evaluate() and honours its decision, so equal
effects get equal gates (spec 09 "Equivalent effects require equivalent
risk and approval logic").

Gate order (spec 09):
  1. task/skill resolution   explicitly requested skill must exist
  2. candidate resolution    done by the router at execution time
  3. context/privacy         credential-looking content in the prompt;
                             project must be registered when a registry
                             is available (FR-02 "never guess")
  4. permission/risk         max(prompt risk, skill risk, task risk) vs
                             policy + autonomy dial; skill permissions
                             requested but not granted
  5. budget hard stop        Token Budget exhaustion goes to approval and
                             never skips any other gate
  6. human approval          one ApprovalRequest bound to the exact action
  7-9. execute, verify, record  (callers: execution/verification modules)

Approval binding (spec 07 and remote spec 17): an approval stores the
sha256 of the canonical action (task params + prompt + routing params)
plus an expiry. authorize_execution() refuses it when it is not approved,
is expired, was already used, or when the stored action no longer hashes
to the approved value (tampered after approval). An approval therefore
cannot be reused for a different action.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .descriptors import SkillDescriptor
from .knowledge import redact_secrets
from .models import Risk, Task
from .policy import PolicyEngine
from .risk import classify_risk
from .state import StateStore
from .token_budget import TokenBudgetManager, hard_stop_approval_reason

_RISK_ORDER = [Risk.LOW, Risk.NORMAL, Risk.HIGH, Risk.VERY_HIGH, Risk.CRITICAL]
DEFAULT_APPROVAL_TTL = timedelta(hours=24)

PASS = "pass"  # nosec B105 - gate outcome label, not a credential
APPROVAL = "approval_required"
DENY = "deny"


@dataclass
class GateResult:
    gate: str
    outcome: str  # pass | approval_required | deny
    reason: str = ""


@dataclass
class GovernanceDecision:
    risk: Risk
    gates: list[GateResult] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return any(g.outcome == DENY for g in self.gates)

    @property
    def requires_approval(self) -> bool:
        return not self.denied and any(g.outcome == APPROVAL for g in self.gates)

    @property
    def allowed(self) -> bool:
        return not self.denied and not self.requires_approval

    @property
    def reason(self) -> str:
        blocking = [g for g in self.gates if g.outcome != PASS]
        return "; ".join(f"[{g.gate}] {g.reason}" for g in blocking) or "all gates passed"

    def to_dict(self) -> dict:
        return {
            "risk": self.risk.value,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "denied": self.denied,
            "gates": [g.__dict__ for g in self.gates],
        }


def _max_risk(*risks: Risk) -> Risk:
    return max(risks, key=_RISK_ORDER.index)


def evaluate(
    task: Task,
    prompt: str,
    store: StateStore,
    policy: PolicyEngine | None = None,
    *,
    skill: SkillDescriptor | None = None,
    requested_skill_id: str | None = None,
    skill_grants: dict[str, list[str]] | None = None,
    project_registry=None,
    budget_manager: TokenBudgetManager | None = None,
) -> GovernanceDecision:
    """Pure decision. It creates nothing and executes nothing. It sets
    task.risk to the effective risk, so a later approval record and the
    executed Task carry the same risk the gates judged."""
    policy = policy or PolicyEngine()
    gates: list[GateResult] = []

    # 1. skill resolution
    if requested_skill_id and skill is None:
        gates.append(GateResult("skill", DENY, f"requested skill '{requested_skill_id}' not found or disabled"))
    else:
        gates.append(GateResult("skill", PASS, skill.id if skill else "role default"))

    # 1b. project policy (D67): what this project may use at all
    project_policy_gates, policy_grants = _project_policy_gates(task, prompt, skill)
    gates += project_policy_gates
    if skill_grants is None:
        skill_grants = policy_grants

    # 3. context / privacy
    _, secret_hits = redact_secrets(prompt)
    if secret_hits:
        gates.append(GateResult(
            "context_privacy", APPROVAL,
            f"prompt contains {secret_hits} credential-like value(s); sending it to an agent needs a human decision",
        ))
    else:
        gates.append(GateResult("context_privacy", PASS))
    if project_registry is not None and project_registry.get(task.project_id) is None:
        gates.append(GateResult("context_project", APPROVAL, f"project '{task.project_id}' is not registered"))

    # 4. permission / risk
    risk = _max_risk(
        classify_risk(task.type, prompt),
        Risk(skill.risk) if skill else Risk.LOW,
        task.risk,
    )
    task.risk = risk
    if policy.requires_approval(risk, autonomy=task.autonomy):
        gates.append(GateResult("risk", APPROVAL, f"effective risk {risk.value}: {prompt[:200]}"))
    else:
        gates.append(GateResult("risk", PASS, risk.value))
    if skill is not None:
        from .skills import SkillRegistry

        _granted, denied = SkillRegistry.effective_permissions(skill, skill_grants)
        if denied:
            gates.append(GateResult(
                "permissions", APPROVAL,
                f"skill '{skill.id}' requests ungranted permission(s): {', '.join(denied)}",
            ))

    # 5. budget hard stop (never used to skip another gate; it only adds one)
    budget_manager = budget_manager or TokenBudgetManager()
    status = budget_manager.check(
        store.get_usage_records(project_id=task.project_id), scope="project", scope_id=task.project_id
    )
    reason = hard_stop_approval_reason(status)
    gates.append(GateResult("budget", APPROVAL, reason) if reason else GateResult("budget", PASS))

    decision = GovernanceDecision(risk=risk, gates=gates)
    store.log_event(
        project_id=task.project_id, task_id=task.task_id, event="governance_evaluated",
        detail=json.dumps({"allowed": decision.allowed, "requires_approval": decision.requires_approval,
                           "denied": decision.denied, "risk": risk.value}),
    )
    return decision


def _project_policy_gates(task: Task, prompt: str, skill: SkillDescriptor | None) -> tuple[list[GateResult], dict | None]:
    """D67. Returns (gates, skill_grants from the policy). No policy entry
    for the project -> no gate (pre-D67 behaviour). An unreadable policy
    file denies everything (fail closed)."""
    from .project_policy import PolicyError, policy_for

    try:
        pol = policy_for(task.project_id)
    except PolicyError as exc:
        return [GateResult("project_policy", DENY, f"project policy file invalid: {exc}")], None
    if pol is None:
        return [], None
    denials: list[str] = []
    if pol.allowed_skills is not None:
        if skill is None:
            denials.append(f"project '{task.project_id}' requires an explicit skill (--skill), one of {pol.allowed_skills}")
        elif skill.id not in pol.allowed_skills:
            denials.append(f"skill '{skill.id}' is not allowed for project '{task.project_id}'")
    if skill is not None:
        from .capability_graph import CapabilityGraph

        graph = CapabilityGraph.load()
        denied_caps = {graph.normalize(c) for c in pol.denied_capabilities}
        hit = sorted({graph.normalize(c) for c in skill.required_capabilities} & denied_caps)
        if hit:
            denials.append(f"skill '{skill.id}' requires capability(ies) denied for this project: {', '.join(hit)}")
        hit_perm = sorted(set(skill.requested_permissions) & set(pol.denied_permissions))
        if hit_perm:
            denials.append(f"skill '{skill.id}' requests permission(s) denied for this project: {', '.join(hit_perm)}")
    pattern = pol.forbidden_match(prompt)
    if pattern:
        denials.append(f"prompt matches a forbidden pattern for this project ({pattern})")
    if pol.max_autonomy is not None and (task.autonomy is None or task.autonomy > pol.max_autonomy):
        task.autonomy = int(pol.max_autonomy)
    if denials:
        return [GateResult("project_policy", DENY, "; ".join(denials))], pol.skill_grants
    return [GateResult("project_policy", PASS, f"policy for '{task.project_id}' satisfied")], pol.skill_grants


# -- approval binding ------------------------------------------------------

def action_hash(action: dict) -> str:
    canonical = json.dumps(action, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def request_approval(
    store: StateStore,
    task: Task,
    decision: GovernanceDecision,
    action: dict,
    ttl: timedelta = DEFAULT_APPROVAL_TTL,
) -> str:
    """Persists a pending approval bound to `action`. `action` must hold
    everything execution will use (kind, prompt, adapter/role, timeout...);
    the Task fields are added here so the Task cannot drift either."""
    request_id = f"appr-{uuid.uuid4().hex[:12]}"
    bound = dict(action)
    bound.update({
        "task_id": task.task_id,
        "goal_id": task.goal_id,
        "project_id": task.project_id,
        "type": task.type,
        "role": task.role,
        "definition_of_done": list(task.definition_of_done),
        "risk": task.risk.value,
    })
    params = dict(bound)
    params["approval_context"] = approval_context(task, decision, action)
    params["action_hash"] = action_hash(bound)
    params["expires_at"] = (datetime.now(timezone.utc) + ttl).isoformat()
    params["gates"] = [g.__dict__ for g in decision.gates if g.outcome != PASS]
    store.create_approval_request(
        request_id=request_id, project_id=task.project_id, risk=task.risk.value,
        reason=decision.reason, task_params=params,
    )
    return request_id


_IRREVERSIBLE_WORDS = ("publish", "release", "force push", "force-push", "deploy", "drop ", "rm -rf",
                       "delete repository", "delete branch", "revoke")


def approval_context(task: Task, decision: GovernanceDecision, action: dict) -> dict:
    """Spec 07: what a human needs to decide. Descriptive only, derived
    from the bound action, so it is not part of the action hash.
    Reversibility is a labelled heuristic, not a guarantee."""
    prompt = str(action.get("prompt", "")).lower()
    if any(w in prompt for w in _IRREVERSIBLE_WORDS):
        reversibility = "likely irreversible or externally visible (heuristic: publication/force/drop/delete words)"
    elif task.risk in (Risk.HIGH, Risk.VERY_HIGH, Risk.CRITICAL):
        reversibility = "possibly destructive; recoverable only via VCS history/backups (heuristic)"
    else:
        reversibility = "expected reversible (heuristic)"
    resources = [f"project:{task.project_id}"]
    try:
        from .registry import ProjectRegistry

        entry = ProjectRegistry().get(task.project_id)
        if entry is not None and entry.repo_path:
            resources.append(f"repo:{entry.repo_path}")
    except Exception:  # noqa: BLE001 - context is best-effort, the approval itself is not
        resources.append("repo: UNKNOWN")
    if action.get("adapter"):
        participant = f"adapter:{action['adapter']}"
    else:
        participant = f"routed by capability (role={task.role}" + (
            f", skill={action['skill_id']})" if action.get("skill_id") else ")")
    budget = next((g.reason for g in decision.gates if g.gate == "budget" and g.outcome != PASS), None)
    return {
        "action": f"{action.get('kind', 'task')}: {str(action.get('prompt', ''))[:200]}",
        "reason": decision.reason,
        "risk": task.risk.value,
        "affected_resources": resources,
        "proposed_participant": participant,
        "reversibility": reversibility,
        "budget_impact": budget or "no budget gate triggered",
        "alternatives": [
            "deny",
            "narrow the request and resubmit",
            "resubmit with --autonomy 0-2 for step-by-step approval",
            "run in an isolated git worktree (run-batch use_worktree) and review before merge",
        ],
    }


@dataclass
class Authorization:
    ok: bool
    reason: str
    action: dict | None = None


_UNBOUND_KEYS = ("action_hash", "expires_at", "gates", "approval_context")


def authorize_execution(store: StateStore, request_id: str, now: datetime | None = None) -> Authorization:
    """The only way an approved action becomes executable. On success the
    request is atomically marked `executed`, so a second call with the
    same id fails (single use)."""
    req = store.get_approval_request(request_id)
    if req is None:
        return Authorization(False, f"no such approval request: {request_id}")
    if req["status"] != "approved":
        return Authorization(False, f"request '{request_id}' is '{req['status']}', not approved")
    params = dict(req["task_params"])
    stored_hash = params.get("action_hash")
    expires_at = params.get("expires_at")
    bound = {k: v for k, v in params.items() if k not in _UNBOUND_KEYS}
    if stored_hash is None:
        # Pre-v0.5 request (created by v0.4 run-task). Still honoured:
        # it was approved by a human under the old rules. It is single-use
        # like the others, and the missing binding is logged.
        store.log_event(project_id=req["project_id"], event="approval_legacy_unbound", detail=request_id)
    elif action_hash(bound) != stored_hash:
        return Authorization(False, "approved action does not match the stored action (tampered or altered)")
    if expires_at is not None:
        now = now or datetime.now(timezone.utc)
        if now > datetime.fromisoformat(expires_at):
            return Authorization(False, f"approval expired at {expires_at}")
    if not store.consume_approval_request(request_id):
        return Authorization(False, "approval already used")
    return Authorization(True, "approved", action=bound)
