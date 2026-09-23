"""Gateway (v0.4 03_Gateway_Runtime/Gateway_Runtime_Contract.md).

Pure-logic core for Phase 2's "Invisible Gateway": builds a normalized
InvocationEnvelope from a caller's request, decides whether it's worth
delegating to Solomon (should_delegate), and assembles the Return
Contract from existing StateStore data once a delegated goal finishes.

Deliberately does NOT wire itself into any Claude Code hook or global
settings.json -- see DECISIONS.md D17/D19 for why activation is a
separate, explicit step the user confirms, not bundled with this module.
This file is the part that is safe to build and test entirely inside the
repo, with no effect on any live Claude Code session until something
outside this repo is told to call it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .registry import ProjectRegistry
from .state import StateStore


def _new_invocation_id() -> str:
    return f"inv-{uuid.uuid4().hex[:12]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatewayMode(str, Enum):
    AUTO = "AUTO"
    FORCE_LOCAL = "FORCE_LOCAL"
    FORCE_SOLOMON = "FORCE_SOLOMON"
    SHADOW = "SHADOW"


@dataclass
class InvocationEnvelope:
    request: str
    caller_type: str
    invocation_id: str = field(default_factory=_new_invocation_id)
    timestamp: str = field(default_factory=_utcnow)
    caller_session_id: str | None = None
    project_hint: str | None = None
    working_directory: str | None = None
    autonomy_override: int | None = None
    privacy_constraints: list[str] = field(default_factory=list)
    expected_output: str | None = None

    def to_dict(self) -> dict:
        return {
            "invocation_id": self.invocation_id,
            "caller_type": self.caller_type,
            "request": self.request,
            "timestamp": self.timestamp,
            "caller_session_id": self.caller_session_id,
            "project_hint": self.project_hint,
            "working_directory": self.working_directory,
            "autonomy_override": self.autonomy_override,
            "privacy_constraints": self.privacy_constraints,
            "expected_output": self.expected_output,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InvocationEnvelope":
        return cls(
            request=data["request"],
            caller_type=data["caller_type"],
            invocation_id=data.get("invocation_id") or _new_invocation_id(),
            timestamp=data.get("timestamp") or _utcnow(),
            caller_session_id=data.get("caller_session_id"),
            project_hint=data.get("project_hint"),
            working_directory=data.get("working_directory"),
            autonomy_override=data.get("autonomy_override"),
            privacy_constraints=list(data.get("privacy_constraints") or []),
            expected_output=data.get("expected_output"),
        )

    def resolve_project_id(self, registry: ProjectRegistry) -> str | None:
        if self.project_hint and registry.get(self.project_hint):
            return self.project_hint
        if self.working_directory:
            entry = registry.detect_from_path(self.working_directory)
            if entry:
                return entry.project_id
        return None


@dataclass
class DelegationDecision:
    delegate: bool
    reason: str
    matched_signals: list[str]


# Gateway_Runtime_Contract.md "Delegation policy" examples, turned into
# keyword signals for a first lean pass -- not a real classifier. A
# UserPromptSubmit hook has a 30s timeout (Phase 0 addendum, D17), which
# rules out an LLM call in this path; this is deliberately cheap and
# approximate, meant to be replaced or scored properly in a later phase.
# Japanese keywords cover this project's actual usage language (CLAUDE.md
# instructions and this session's own prompts are in Japanese).
_DELEGATE_SIGNALS: dict[str, tuple[str, ...]] = {
    "multi-file / cross-file change": ("across files", "refactor", "複数ファイル", "全体的に", "リファクタ"),
    "implementation + test + review": ("implement and test", "then review", "実装して", "テストも", "レビューも"),
    "multiple specialist roles": ("coder and reviewer", "実装してからレビュー", "デバッグして"),
    "explicit project goal continuation": ("continue the", "続き", "前回の", "前のタスク"),
    "long-running / multi-step": ("step by step", "段階的に", "フェーズ", "順番に"),
}
_LOCAL_SIGNALS: dict[str, tuple[str, ...]] = {
    "tiny explanation": ("explain", "what is", "とは何", "って何", "説明して"),
    "isolated wording question": ("rename this one", "この単語", "この変数名"),
    "trivial one-file edit": ("this one line", "typo", "この1行"),
}


# CLAUDE.md's own routing rule explicitly exempts Solomon's own
# development ("solomon_ai_orchestrator プロジェクト自身の開発...Solomonの
#自己参照になり本末転倒なため...このチャットで直接行う") -- the keyword
# heuristic below has no way to know that, so it is hardcoded here rather
# than left to accidentally recommend routing Solomon through itself.
_SELF_REFERENTIAL_PROJECT_IDS = {"solomon_ai_orchestrator"}


def should_delegate(
    envelope: InvocationEnvelope,
    mode: GatewayMode = GatewayMode.AUTO,
    project_id: str | None = None,
) -> DelegationDecision:
    if mode == GatewayMode.FORCE_LOCAL:
        return DelegationDecision(False, "FORCE_LOCAL mode", [])

    if project_id in _SELF_REFERENTIAL_PROJECT_IDS:
        return DelegationDecision(False, "self-referential project, CLAUDE.md exempts it from routing", [])

    if mode == GatewayMode.FORCE_SOLOMON:
        return DelegationDecision(True, "FORCE_SOLOMON mode", [])

    if project_id is None:
        return DelegationDecision(False, "no registered project matched (FR-02: never guess)", [])

    text = envelope.request.lower()
    matched = [label for label, kws in _DELEGATE_SIGNALS.items() if any(kw.lower() in text for kw in kws)]
    local_matched = [label for label, kws in _LOCAL_SIGNALS.items() if any(kw.lower() in text for kw in kws)]
    delegate = bool(matched) and not local_matched

    if mode == GatewayMode.SHADOW:
        reason = "SHADOW mode: would have " + ("delegated" if delegate else "stayed local")
        return DelegationDecision(False, reason, matched or local_matched)

    if delegate:
        return DelegationDecision(True, "matched delegate signal(s): " + ", ".join(matched), matched)
    if local_matched:
        return DelegationDecision(False, "matched local signal(s): " + ", ".join(local_matched), local_matched)
    return DelegationDecision(False, "no delegate signal matched, default to local", [])


def build_return_contract(goal_id: str, store: StateStore) -> dict:
    from .usage import UsageManager

    goal = store.get_goal(goal_id)
    if goal is None:
        return {
            "goal_id": goal_id, "status": "UNKNOWN", "summary": "no such goal recorded",
            "tasks": [], "changed_artifacts": [], "verification_state": "UNKNOWN",
            "approvals": [], "usage": None, "log_references": [],
        }

    project_id = goal["project_id"]
    tasks = [t for t in store.list_tasks(project_id=project_id) if t.get("goal_id") == goal_id]
    approvals = store.list_approval_requests(project_id=project_id, status="pending")
    usage = UsageManager(store).get_project_usage(project_id)
    events = [e for e in store.list_events_v04(project_id=project_id) if e.get("goal_id") == goal_id]

    statuses = {t["status"] for t in tasks}
    if not tasks:
        verification_state = "NO_TASKS"
    elif statuses <= {"COMPLETE"}:
        verification_state = "VERIFIED"
    elif "REMEDIATION_REQUIRED" in statuses:
        verification_state = "REMEDIATION_REQUIRED"
    elif "UNKNOWN" in statuses:
        verification_state = "NEEDS_REVIEW"
    elif statuses & {"FAILED", "BLOCKED"}:
        verification_state = "BLOCKED"
    else:
        verification_state = "IN_PROGRESS"

    return {
        "goal_id": goal_id,
        "status": goal["status"],
        "summary": goal["text"],
        "tasks": [{"task_id": t["task_id"], "status": t["status"], "role": t["role"]} for t in tasks],
        "changed_artifacts": [],
        "verification_state": verification_state,
        "approvals": [{"request_id": a["request_id"], "risk": a["risk"], "reason": a["reason"]} for a in approvals],
        "usage": usage,
        "log_references": [e["event_id"] for e in events],
    }
