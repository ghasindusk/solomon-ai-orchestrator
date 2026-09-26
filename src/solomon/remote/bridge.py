"""Remote Task Bridge (Octavryn SI v0.5 remote extension, specs 15/17/20).

A control plane, not a remote shell. In v0.5 this is an in-process
interface with no network listener. Production transport is a v0.6 gate
(spec 19/20: "Do not expose a public internet listener ... to demonstrate
v0.5"). A future transport only has to call submit()/approve()/status()
with the raw payload and signature it received.

Checks on submit(), in order, each failing closed:
  identity known -> HMAC signature valid -> not revoked -> rate limit ->
  envelope valid -> not expired -> not a replay -> project in identity
  scope (and registered) -> governance.evaluate() (Context Firewall,
  risk/approval, skill permissions, Token Budget) -> remote constraints
  (a "deny" category turns an approval-needing action into BLOCKED)
Only then is the request queued. If the Worker is offline it stays
OFFLINE in the durable queue. The bridge never claims execution happened.

Identities and keys are supplied by the caller (IdentityStore). This
module never generates, stores or rotates real credentials: issuing
credentials is a human-approval gate (spec 17), so production key
management is v0.6 work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..descriptors import SkillDescriptor
from ..governance import action_hash, authorize_execution, evaluate, request_approval
from ..models import Risk, Task
from ..policy import PolicyEngine
from ..state import StateStore
from .protocol import ProtocolError, RemoteStatus, TaskEnvelope
from . import worker as wk

_PUBLICATION_WORDS = ("publish", "release", "push", "deploy", "upload", "公開")
_AUTONOMY_DIAL = {"supervised": 1, "bounded": 2, "delegated": None}


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign(payload: dict, key: bytes) -> str:
    return hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()


def _verify(payload: dict, signature: str | None, key: bytes) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(payload, key), str(signature))


@dataclass
class Identity:
    identity_ref: str
    key: bytes
    projects: set[str] = field(default_factory=set)  # "*" = all registered projects
    can_approve: bool = False
    revoked: bool = False

    def allows(self, project_id: str) -> bool:
        return "*" in self.projects or project_id in self.projects


class IdentityStore:
    def __init__(self, identities: list[Identity] | None = None):
        self._by_ref = {i.identity_ref: i for i in identities or []}

    def get(self, ref: str | None) -> Identity | None:
        return self._by_ref.get(ref) if ref else None

    def revoke(self, ref: str) -> bool:
        ident = self._by_ref.get(ref)
        if ident is None:
            return False
        ident.revoked = True
        return True


@dataclass
class BridgeResponse:
    status: RemoteStatus
    reason: str
    request_id: str | None = None
    task_id: str | None = None
    approval_id: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status.value, "reason": self.reason, "request_id": self.request_id,
                "task_id": self.task_id, "approval_id": self.approval_id}


class RateLimiter:
    def __init__(self, max_requests: int = 30, window: timedelta = timedelta(minutes=1)):
        self.max_requests = max_requests
        self.window = window
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: datetime) -> bool:
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_requests:
            return False
        q.append(now)
        return True


class TaskBridge:
    def __init__(self, store: StateStore, identities: IdentityStore, worker: wk.Worker,
                 policy: PolicyEngine | None = None, project_registry=None,
                 rate_limiter: RateLimiter | None = None):
        self.store = store
        self.identities = identities
        self.worker = worker
        self.policy = policy or PolicyEngine()
        self.project_registry = project_registry
        self.rate = rate_limiter or RateLimiter()
        wk.ensure_schema(store)

    # -- submit ----------------------------------------------------------------

    def _reject(self, reason: str, request_id: str | None = None) -> BridgeResponse:
        self.store.log_event(project_id="-", event="remote_rejected", detail=f"{request_id}:{reason}")
        return BridgeResponse(RemoteStatus.REJECTED, reason, request_id=request_id)

    def submit(self, envelope: dict, signature: str | None, now: datetime | None = None) -> BridgeResponse:
        now = now or datetime.now(timezone.utc)
        if self.killed:
            return self._reject("remote bridge kill switch engaged")
        claimed = ((envelope or {}).get("actor") or {}).get("identity_ref") if isinstance(envelope, dict) else None
        ident = self.identities.get(claimed)
        if ident is None or not _verify(envelope, signature, ident.key):
            return self._reject("unauthenticated")
        if ident.revoked:
            return self._reject("identity revoked")
        if not self.rate.allow(ident.identity_ref, now):
            return self._reject("rate limited")
        try:
            env = TaskEnvelope.parse(envelope)
        except ProtocolError as exc:
            return self._reject(f"invalid envelope: {exc}")
        if not env.is_live(now):
            return self._reject("expired or not yet valid", env.request_id)
        if wk.seen_request(self.store, env.request_id):
            return self._reject("replayed request_id", env.request_id)
        if not ident.allows(env.project_id):
            return self._record_rejected(env, envelope, "project not in identity scope")
        if self.project_registry is not None and self.project_registry.get(env.project_id) is None:
            return self._record_rejected(env, envelope, "project not registered")
        if env.continuation_task_id:
            prior = self.store.get_task(env.continuation_task_id)
            if prior is None or prior["project_id"] != env.project_id:
                return self._record_rejected(env, envelope, "continuation task not found in this project")

        task = Task(goal_id=env.correlation_id, project_id=env.project_id, type="remote", role=env.role,
                    definition_of_done=["result_recorded"], autonomy=_AUTONOMY_DIAL[env.constraints.autonomy])
        skill = self._skill_for(task, env)
        self.store.save_task(task)
        decision = evaluate(task, env.goal, self.store, self.policy, skill=skill,
                            project_registry=self.project_registry)
        self.store.save_task(task)

        if decision.denied:
            return self._record(env, envelope, task, RemoteStatus.BLOCKED, decision.reason)
        if decision.requires_approval:
            category = "external_publication" if self._is_publication(env.goal) else "destructive_actions"
            if task.risk in (Risk.HIGH, Risk.VERY_HIGH, Risk.CRITICAL) and getattr(env.constraints, category) == "deny":
                return self._record(env, envelope, task, RemoteStatus.BLOCKED,
                                    f"{category} is 'deny' for this request; {decision.reason}")
            approval_id = request_approval(self.store, task, decision, {
                "kind": "route", "prompt": env.goal, "timeout": 600, "max_attempts": 2,
                "remote_request_id": env.request_id,
                "skill_locality": "local" if env.constraints.locality == "local" else None,
            })
            return self._record(env, envelope, task, RemoteStatus.WAITING_APPROVAL, decision.reason,
                                approval_id=approval_id)
        status = RemoteStatus.QUEUED if self.worker.is_online(now) else RemoteStatus.OFFLINE
        return self._record(env, envelope, task, status,
                            "queued for the Worker" if status == RemoteStatus.QUEUED
                            else "Worker offline; queued durably, not executed")

    @staticmethod
    def _is_publication(goal: str) -> bool:
        g = goal.lower()
        return any(w in g for w in _PUBLICATION_WORDS)

    def _skill_for(self, task: Task, env: TaskEnvelope) -> SkillDescriptor | None:
        from ..router import Router

        skill = Router().skill_for_task(task)
        if skill is not None and env.constraints.locality == "local":
            from dataclasses import replace

            skill = replace(skill, locality_constraint="local")
        return skill

    def _record(self, env, raw, task, status, reason, approval_id=None) -> BridgeResponse:
        wk.record_request(self.store, request_id=env.request_id, correlation_id=env.correlation_id,
                          identity_ref=env.actor_identity_ref, project_id=env.project_id, status=status.value,
                          reason=reason, envelope=raw, task_id=task.task_id, approval_id=approval_id)
        skill = self._skill_for(task, env)
        wk.save_checkpoint(self.store, env.request_id, task.task_id, status.value, {
            "reason": reason,
            "pending_work": env.goal[:200] if status not in (RemoteStatus.BLOCKED,) else None,
            "affected_resources": [f"project:{env.project_id}"],
            "skill": skill.id if skill else None,
            "approval": {"approval_id": approval_id, "state": "pending"} if approval_id else None,
            "verification": "not started",
            "rollback": "nothing executed yet; nothing to roll back",
        })
        return BridgeResponse(status, reason, request_id=env.request_id, task_id=task.task_id,
                              approval_id=approval_id)

    def _record_rejected(self, env, raw, reason) -> BridgeResponse:
        # Authenticated but out of scope: recorded, so the request_id is
        # burned and cannot be replayed against a different project later.
        wk.record_request(self.store, request_id=env.request_id, correlation_id=env.correlation_id,
                          identity_ref=env.actor_identity_ref, project_id=env.project_id,
                          status=RemoteStatus.REJECTED.value, reason=reason, envelope=raw)
        return self._reject(reason, env.request_id)

    # -- approvals -------------------------------------------------------------

    def approve(self, message: dict, signature: str | None, now: datetime | None = None) -> BridgeResponse:
        """message: {approval_id, action_hash, decision: approve|deny,
        identity_ref, expires_at}. Binds to the exact stored action; an
        approval for a different/changed action is refused."""
        now = now or datetime.now(timezone.utc)
        if self.killed:
            return self._reject("remote bridge kill switch engaged")
        ident = self.identities.get((message or {}).get("identity_ref"))
        if ident is None or not _verify(message, signature, ident.key):
            return self._reject("unauthenticated approval")
        if ident.revoked:
            return self._reject("identity revoked")
        if not ident.can_approve:
            return self._reject("identity is not allowed to approve")
        try:
            if now > datetime.fromisoformat(message["expires_at"]):
                return self._reject("approval message expired")
        except (KeyError, TypeError, ValueError):
            return self._reject("approval message needs a valid expires_at")
        req = self.store.get_approval_request(message.get("approval_id", ""))
        if req is None or req["status"] != "pending":
            return self._reject("no pending approval with that id")
        if not ident.allows(req["project_id"]):
            return self._reject("approval outside identity project scope")
        if message.get("action_hash") != req["task_params"].get("action_hash"):
            return self._reject("action_hash does not match the pending action")
        approved = message.get("decision") == "approve"
        self.store.decide_approval_request(req["request_id"], approved=approved,
                                           note="remote", decided_by=f"human:{ident.identity_ref}")
        remote_id = req["task_params"].get("remote_request_id")
        if remote_id:
            new_status = RemoteStatus.QUEUED if approved else RemoteStatus.BLOCKED
            wk.update_request(self.store, remote_id, new_status.value,
                              "approved; waiting for the Worker" if approved else "denied by human")
            wk.save_checkpoint(self.store, remote_id, req["task_params"].get("task_id"), new_status.value,
                               {"approval_id": req["request_id"], "decided_by": ident.identity_ref})
        return BridgeResponse(RemoteStatus.QUEUED if approved else RemoteStatus.BLOCKED,
                              "approved" if approved else "denied", request_id=remote_id,
                              approval_id=req["request_id"])

    # -- kill switch (remote spec 17 "provide revoke/kill capability") ----------

    KILL_KEY = "bridge_kill_switch"

    @property
    def killed(self) -> bool:
        # Missing = not engaged; any value other than "released" = engaged (fail closed).
        return wk.get_control(self.store, self.KILL_KEY) not in (None, "released")

    def kill(self, reason: str, by: str) -> int:
        """Local operator action: stop all remote work. New submissions and
        approvals are rejected, the worker processes nothing, and every
        not-yet-started request is BLOCKED. Persisted across restarts."""
        if not reason or not by:
            raise ValueError("kill needs a reason and an identity")
        wk.set_control(self.store, self.KILL_KEY, "engaged", reason, by)
        blocked = 0
        for status in (RemoteStatus.QUEUED, RemoteStatus.OFFLINE, RemoteStatus.WAITING_APPROVAL):
            for r in wk.list_requests(self.store, status=status.value):
                wk.update_request(self.store, r["request_id"], RemoteStatus.BLOCKED.value, f"kill switch: {reason}")
                blocked += 1
        return blocked

    def release_kill(self, reason: str, by: str) -> None:
        if not reason or not by:
            raise ValueError("release needs a reason and a human identity")
        wk.set_control(self.store, self.KILL_KEY, "released", reason, by)

    # -- revocation ------------------------------------------------------------

    def revoke(self, identity_ref: str) -> int:
        """Revokes an identity and blocks its not-yet-started work.
        Returns how many queued requests were blocked."""
        self.identities.revoke(identity_ref)
        blocked = 0
        for status in (RemoteStatus.QUEUED, RemoteStatus.OFFLINE, RemoteStatus.WAITING_APPROVAL):
            for r in wk.list_requests(self.store, status=status.value, identity_ref=identity_ref):
                wk.update_request(self.store, r["request_id"], RemoteStatus.BLOCKED.value, "identity revoked")
                blocked += 1
        self.store.log_event(project_id="-", event="remote_identity_revoked", detail=identity_ref)
        return blocked

    # -- worker processing -------------------------------------------------------

    def process_queue(self, adapter_factory, router=None, now: datetime | None = None) -> list[BridgeResponse]:
        """Runs queued work if the Worker is online. Offline requests stay
        OFFLINE until a Worker is online, then they are picked up."""
        from ..execution import execute_with_fallback
        from ..router import Router

        if self.killed or not self.worker.is_online(now):
            return []
        router = router or Router(state=self.store)
        out = []
        pending = (wk.list_requests(self.store, status=RemoteStatus.QUEUED.value)
                   + wk.list_requests(self.store, status=RemoteStatus.OFFLINE.value))
        for req in pending:
            ident = self.identities.get(req["identity_ref"])
            if ident is None or ident.revoked:
                wk.update_request(self.store, req["request_id"], RemoteStatus.BLOCKED.value, "identity revoked")
                out.append(BridgeResponse(RemoteStatus.BLOCKED, "identity revoked", req["request_id"]))
                continue
            task = Task.from_schema_dict(self.store.get_task(req["task_id"]))
            env = TaskEnvelope.parse(req["envelope"])
            skill = self._skill_for(task, env)
            authorized = False
            if req["approval_id"]:
                auth = authorize_execution(self.store, req["approval_id"])
                if not auth.ok:
                    wk.update_request(self.store, req["request_id"], RemoteStatus.BLOCKED.value, auth.reason)
                    out.append(BridgeResponse(RemoteStatus.BLOCKED, auth.reason, req["request_id"]))
                    continue
                authorized = True
            self.worker.transition(wk.WorkerState.BUSY)
            wk.update_request(self.store, req["request_id"], RemoteStatus.RUNNING.value)
            wk.save_checkpoint(self.store, req["request_id"], task.task_id, RemoteStatus.RUNNING.value,
                               {"skill": skill.id if skill else None})
            try:
                outcome = execute_with_fallback(task, env.goal, router, adapter_factory, state=self.store,
                                                policy=self.policy, authorized=authorized, skill=skill)
            finally:
                self.worker.transition(wk.WorkerState.READY)
            out.append(self._finish(req, task, outcome))
        return out

    def _finish(self, req, task, outcome) -> BridgeResponse:
        return finish_request(self.store, req["request_id"], task, outcome)

    # -- status (continuity) -----------------------------------------------------

    def status(self, query: dict, signature: str | None) -> dict:
        """query: {identity_ref, request_id | correlation_id}. Answers from
        durable state, so any surface gets the same answer."""
        ident = self.identities.get((query or {}).get("identity_ref"))
        if ident is None or not _verify(query, signature, ident.key) or ident.revoked:
            return {"status": RemoteStatus.REJECTED.value, "reason": "unauthenticated"}
        rid = query.get("request_id")
        if not rid and query.get("correlation_id"):
            for r in wk.list_requests(self.store, identity_ref=ident.identity_ref):
                if r["correlation_id"] == query["correlation_id"]:
                    rid = r["request_id"]
        req = wk.get_request(self.store, rid) if rid else None
        if req is None or not ident.allows(req["project_id"]):
            return {"status": "unknown", "reason": "no such request in scope"}
        cps = wk.checkpoints_for(self.store, req["request_id"])
        verified = [c for c in cps if c["status"] == RemoteStatus.VERIFIED.value]
        blockers = [req["reason"]] if req["status"] in ("blocked", "waiting_approval", "failed") else []
        next_action = {
            "waiting_approval": "a human decides the pending approval",
            "offline": "start the Octavryn Worker",
            "queued": "Worker will pick it up",
            "blocked": "review the blocker; resubmit with a new request_id if appropriate",
            "failed": "inspect attempts; retry with a new request",
        }.get(req["status"], "none")
        return {
            "request_id": req["request_id"],
            "correlation_id": req["correlation_id"],
            "project_id": req["project_id"],
            "task_id": req["task_id"],
            "status": req["status"],
            "reason": req["reason"],
            "checkpoints": [{"id": c["checkpoint_id"], "status": c["status"]} for c in cps],
            "last_verified_checkpoint": verified[-1]["checkpoint_id"] if verified else None,
            "approval_id": req["approval_id"],
            "worker_online": self.worker.is_online(),
            "blockers": blockers,
            "next_safe_action": next_action,
            "phase": req["status"],
            "completion_pct": self._completion_pct(req),
            "available_participants": self._available_participants(),
            "latest_tests": self._latest_tests(req),
            "kill_switch_engaged": self.killed,
        }

    def _completion_pct(self, req) -> float | None:
        """Share of this request's goal tasks that passed their Definition of
        Done. None (UNKNOWN) when no task exists yet."""
        tasks = [t for t in self.store.list_tasks(project_id=req["project_id"])
                 if t.get("goal_id") == req["correlation_id"]]
        if not tasks:
            return None
        return round(100.0 * sum(1 for t in tasks if t["status"] == "COMPLETE") / len(tasks), 1)

    def _available_participants(self) -> list[str] | None:
        """From the persisted registry (no adapter CLI is spawned). None =
        registry never refreshed (UNKNOWN), not 'nobody'."""
        rows = self.store.list_intelligences()
        if not rows:
            return None
        return sorted(r["intelligence_id"] for r in rows if r["availability"] == "available")

    def _latest_tests(self, req):
        if not req["task_id"]:
            return None
        results = self.store.get_task_results(req["task_id"])
        return results[-1].get("tests") if results else None


def finish_request(store: StateStore, request_id: str, task: Task, outcome) -> BridgeResponse:
    """Records the result of executing a remote request, whichever local
    path executed it (bridge worker or `octavryn execute-approved`), so the
    remote status always reflects what actually happened."""
    if outcome.blocked is not None:
        wk.update_request(store, request_id, RemoteStatus.BLOCKED.value, outcome.blocked.reason)
        return BridgeResponse(RemoteStatus.BLOCKED, outcome.blocked.reason, request_id, task.task_id)
    tried = [a.adapter_name for a in outcome.attempts]
    result = outcome.final_result
    req = wk.get_request(store, request_id)
    data = {
        "attempts": tried,
        "participants": [a.adapter_name for a in outcome.attempts if not a.skipped],
        "affected_resources": [f"project:{req['project_id']}"] if req else [],
        "approval": {"approval_id": req["approval_id"], "state": "consumed"} if req and req["approval_id"] else None,
        "rollback": "no automatic rollback; repository changes are reverted through VCS history",
    }
    if result is not None and tried and result.agent != tried[0]:
        data["substituted_from"] = tried[0]
        data["substituted_to"] = result.agent
    if result is None or result.status == "FAILED":
        status, reason = RemoteStatus.FAILED, "no adapter produced a successful result"
    else:
        final_task = store.get_task(task.task_id)
        verified = final_task is not None and final_task["status"] == "COMPLETE"
        status = RemoteStatus.VERIFIED if verified else RemoteStatus.COMPLETED
        reason = "Definition of Done verified" if verified else "result recorded; not yet verified"
        data["verification"] = final_task["status"] if final_task else "UNKNOWN"
    wk.update_request(store, request_id, status.value, reason)
    wk.save_checkpoint(store, request_id, task.task_id, status.value, data)
    return BridgeResponse(status, reason, request_id, task.task_id)


def approval_message(identity_ref: str, approval_id: str, store: StateStore, decision: str = "approve",
                     ttl: timedelta = timedelta(minutes=10)) -> dict:
    """Client-side helper: builds the message a human's surface signs."""
    req = store.get_approval_request(approval_id)
    return {
        "identity_ref": identity_ref,
        "approval_id": approval_id,
        "action_hash": req["task_params"]["action_hash"] if req else None,
        "decision": decision,
        "expires_at": (datetime.now(timezone.utc) + ttl).isoformat(),
    }


__all__ = ["TaskBridge", "IdentityStore", "Identity", "RateLimiter", "BridgeResponse", "sign",
           "approval_message", "action_hash"]
