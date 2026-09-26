"""Octavryn SI v0.5 R11 Remote Control Foundation -- the required tests
from remote spec 20: reject unauthenticated / expired / replayed /
wrong-project requests; privileged actions become WAITING_APPROVAL or
BLOCKED; approvals cannot be reused; an offline Worker never reports
completion; provider substitution obeys capabilities/policy; switching
surfaces preserves durable state; remote paths cannot bypass the
Context Firewall / Token Budget / verification / approval; revocation
prevents new work."""

import sys
import pathlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.remote.bridge import Identity, IdentityStore, RateLimiter, TaskBridge, approval_message, sign
from solomon.remote.protocol import ProtocolError, RemoteStatus, TaskEnvelope, new_envelope
from solomon.remote.worker import InvalidTransition, Worker, WorkerState
from solomon.result import TaskResult
from solomon.router import Router
from solomon.state import StateStore

KEY = b"test-key-owner-000000000000000000"
OTHER_KEY = b"test-key-other-000000000000000000"
SAFE = "summarize the project notes"


class Fake(AgentAdapter):
    def __init__(self, name, ok=True, available=True):
        self.name, self.ok, self.available = name, ok, available
        self.calls = 0

    def health(self):
        return AdapterHealth(self.available, "fake")

    def execute(self, task, prompt, timeout_s=600):
        self.calls += 1
        now = self._now()
        return TaskResult(task_id=task.task_id, status="RESULT_RECEIVED" if self.ok else "FAILED",
                          summary="ok", agent=self.name, started_at=now, finished_at=now,
                          uncertainties=[] if self.ok else ["timeout"])


@pytest.fixture
def world(tmp_path):
    store = StateStore(tmp_path / "r.sqlite3")
    ids = IdentityStore([
        Identity("owner", KEY, projects={"p1"}, can_approve=True),
        Identity("viewer", OTHER_KEY, projects={"p1"}, can_approve=False),
    ])
    worker = Worker(store)
    bridge = TaskBridge(store, ids, worker)
    adapters = {n: Fake(n) for n in ("claude_code", "codex", "antigravity", "localai_ollama")}
    return store, bridge, worker, adapters


def safe_env(ident="owner", project="p1", goal=SAFE, **kw):
    """Routine work declares `bounded` autonomy; the protocol default is
    `supervised`, under which even NORMAL-risk work waits for approval."""
    c = dict(kw.pop("constraints", {}) or {})
    c.setdefault("autonomy", "bounded")
    return new_envelope(ident, project, goal, constraints=c, **kw)


def test_supervised_default_sends_normal_work_to_approval(world):
    _, bridge, _, adapters = world
    r = submit(bridge, new_envelope("owner", "p1", SAFE))
    assert r.status == RemoteStatus.WAITING_APPROVAL
    assert sum(a.calls for a in adapters.values()) == 0


def submit(bridge, env, key=KEY, now=None):
    return bridge.submit(env, sign(env, key), now=now)


def run_queue(bridge, adapters):
    return bridge.process_queue(lambda name, cwd=None: adapters[name], router=Router(state=bridge.store))


# --- authentication / validity ----------------------------------------------------

def test_unauthenticated_rejected(world):
    _, bridge, _, _ = world
    env = new_envelope("owner", "p1", SAFE)
    assert bridge.submit(env, None).status == RemoteStatus.REJECTED
    assert bridge.submit(env, "00" * 32).status == RemoteStatus.REJECTED
    assert submit(bridge, env, key=OTHER_KEY).status == RemoteStatus.REJECTED
    assert submit(bridge, new_envelope("stranger", "p1", SAFE)).status == RemoteStatus.REJECTED


def test_tampered_envelope_rejected(world):
    _, bridge, _, _ = world
    env = new_envelope("owner", "p1", SAFE)
    sig = sign(env, KEY)
    env["goal"] = "force push to main"
    assert bridge.submit(env, sig).status == RemoteStatus.REJECTED


def test_expired_rejected(world):
    _, bridge, _, _ = world
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    env = new_envelope("owner", "p1", SAFE, now=past, ttl=timedelta(minutes=5))
    r = submit(bridge, env)
    assert r.status == RemoteStatus.REJECTED and "expired" in r.reason


def test_replay_rejected(world):
    _, bridge, _, _ = world
    env = safe_env()
    assert submit(bridge, env).status in (RemoteStatus.QUEUED, RemoteStatus.OFFLINE)
    r = submit(bridge, env)
    assert r.status == RemoteStatus.REJECTED and "replay" in r.reason


def test_wrong_project_rejected_and_request_id_burned(world):
    _, bridge, _, _ = world
    env = safe_env(project="secret_project")
    assert submit(bridge, env).status == RemoteStatus.REJECTED
    env["project_id"] = "p1"  # same request_id retargeted
    assert "replay" in submit(bridge, env).reason


def test_protocol_validation_fails_closed():
    base = new_envelope("owner", "p1", SAFE)
    for mutate in (
        lambda e: e.update(schema_version="0.4"),
        lambda e: e["actor"].update(type="ai"),
        lambda e: e.update(request_id="not-a-uuid"),
        lambda e: e.update(goal="   "),
        lambda e: e["constraints"].update(autonomy="yolo"),
        lambda e: e.update(expires_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat()),
        lambda e: e.update(created_at="2026-09-24T00:00:00"),  # no timezone
    ):
        e = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
        mutate(e)
        with pytest.raises(ProtocolError):
            TaskEnvelope.parse(e)


def test_rate_limit(world):
    store, _, worker, _ = world
    bridge = TaskBridge(store, IdentityStore([Identity("owner", KEY, {"p1"})]), worker,
                        rate_limiter=RateLimiter(max_requests=2))
    rs = [submit(bridge, safe_env()).status for _ in range(3)]
    assert rs[-1] == RemoteStatus.REJECTED


# --- governance on the remote path --------------------------------------------------

def test_privileged_action_with_approval_policy_waits_for_approval(world):
    _, bridge, _, adapters = world
    env = new_envelope("owner", "p1", "delete the stale cache directory",
                       constraints={"destructive_actions": "approval_required"})
    r = submit(bridge, env)
    assert r.status == RemoteStatus.WAITING_APPROVAL and r.approval_id
    assert sum(a.calls for a in adapters.values()) == 0


def test_privileged_action_with_deny_policy_is_blocked(world):
    _, bridge, _, _ = world
    r = submit(bridge, new_envelope("owner", "p1", "publish the release to GitHub"))
    assert r.status == RemoteStatus.BLOCKED and "external_publication" in r.reason


def test_credential_in_goal_hits_context_firewall(world):
    _, bridge, _, _ = world
    env = new_envelope("owner", "p1", "use api_key=abcdefghijklmnopqrstuvwxyz to fetch",
                       constraints={"destructive_actions": "approval_required"})
    r = submit(bridge, env)
    assert r.status == RemoteStatus.WAITING_APPROVAL
    assert "context_privacy" in r.reason


def test_token_budget_applies_to_remote(world, monkeypatch):
    from solomon import governance
    from solomon.token_budget import TokenBudgetStatus

    class Exhausted:
        def check(self, records, scope, scope_id):
            return TokenBudgetStatus(scope=scope, scope_id=scope_id, level="hard_stop", pct_used=120.0,
                                     limit_tokens=1, total_tokens=2, known_record_count=1, unknown_record_count=0)

    monkeypatch.setattr(governance, "TokenBudgetManager", lambda: Exhausted())
    _, bridge, _, _ = world
    r = submit(bridge, new_envelope("owner", "p1", SAFE, constraints={"destructive_actions": "approval_required"}))
    assert r.status == RemoteStatus.WAITING_APPROVAL and "budget" in r.reason


# --- approvals ------------------------------------------------------------------------

def test_approval_requires_can_approve_and_exact_action(world):
    store, bridge, worker, adapters = world
    env = new_envelope("owner", "p1", "delete the stale cache directory",
                       constraints={"destructive_actions": "approval_required"})
    r = submit(bridge, env)
    msg = approval_message("viewer", r.approval_id, store)
    assert bridge.approve(msg, sign(msg, OTHER_KEY)).status == RemoteStatus.REJECTED
    bad = approval_message("owner", r.approval_id, store)
    bad["action_hash"] = "0" * 64
    assert bridge.approve(bad, sign(bad, KEY)).status == RemoteStatus.REJECTED
    ok = approval_message("owner", r.approval_id, store)
    assert bridge.approve(ok, sign(ok, KEY)).status == RemoteStatus.QUEUED
    assert store.get_approval_request(r.approval_id)["decided_by"] == "human:owner"
    # cannot be decided twice
    assert bridge.approve(ok, sign(ok, KEY)).status == RemoteStatus.REJECTED
    worker.start()
    done = run_queue(bridge, adapters)
    assert done[0].status in (RemoteStatus.COMPLETED, RemoteStatus.VERIFIED)
    assert store.get_approval_request(r.approval_id)["status"] == "executed"


def test_approval_cannot_be_reused_for_second_execution(world):
    store, bridge, worker, adapters = world
    r = submit(bridge, new_envelope("owner", "p1", "delete the stale cache directory",
                                    constraints={"destructive_actions": "approval_required"}))
    msg = approval_message("owner", r.approval_id, store)
    bridge.approve(msg, sign(msg, KEY))
    worker.start()
    run_queue(bridge, adapters)
    calls = sum(a.calls for a in adapters.values())
    from solomon.remote import worker as wk
    wk.update_request(store, r.request_id, "queued")  # attacker re-queues the same request
    again = run_queue(bridge, adapters)
    assert again[0].status == RemoteStatus.BLOCKED
    assert sum(a.calls for a in adapters.values()) == calls


def test_expired_approval_message_rejected(world):
    store, bridge, _, _ = world
    r = submit(bridge, new_envelope("owner", "p1", "delete the stale cache directory",
                                    constraints={"destructive_actions": "approval_required"}))
    msg = approval_message("owner", r.approval_id, store, ttl=timedelta(seconds=-1))
    assert bridge.approve(msg, sign(msg, KEY)).status == RemoteStatus.REJECTED


# --- worker / offline / verification ------------------------------------------------------

def test_offline_worker_never_reports_completion(world):
    store, bridge, worker, adapters = world
    r = submit(bridge, safe_env())
    assert r.status == RemoteStatus.OFFLINE
    assert run_queue(bridge, adapters) == []
    q = {"identity_ref": "owner", "request_id": r.request_id}
    st = bridge.status(q, sign(q, KEY))
    assert st["status"] == "offline" and st["last_verified_checkpoint"] is None
    assert sum(a.calls for a in adapters.values()) == 0
    worker.start()
    done = run_queue(bridge, adapters)
    assert done[0].status in (RemoteStatus.COMPLETED, RemoteStatus.VERIFIED)


def test_stale_heartbeat_counts_as_offline(world):
    _, _, worker, _ = world
    worker.start(now=datetime.now(timezone.utc) - timedelta(hours=1))
    assert worker.state == WorkerState.READY
    assert worker.is_online() is False


def test_worker_state_machine_rejects_invalid_transition(world):
    _, _, worker, _ = world
    with pytest.raises(InvalidTransition):
        worker.transition(WorkerState.BUSY)  # OFFLINE -> BUSY
    worker.start()
    worker.stop()
    assert worker.state == WorkerState.OFFLINE


def test_completed_vs_verified_are_distinct(world):
    store, bridge, worker, adapters = world
    worker.start()
    submit(bridge, safe_env())
    done = run_queue(bridge, adapters)[0]
    task = store.get_task(done.task_id)
    expected = RemoteStatus.VERIFIED if task["status"] == "COMPLETE" else RemoteStatus.COMPLETED
    assert done.status == expected


def test_provider_substitution_is_recorded_and_capability_bound(world):
    """First-ranked adapter fails with a retryable error; fallback goes to the
    next capability-eligible adapter, and the substitution is recorded. The
    local model lacks `coding`, so it is never a substitute for a coder task."""
    from solomon.router import AgentScore

    store, bridge, worker, adapters = world
    worker.start()
    adapters["claude_code"].ok = False  # FAILED + "timeout" -> retryable

    class Pinned(Router):
        def route(self, task, health_checks=None, gpu_telemetry=None, skill=None):
            eligible = self.candidates_for_role(task.role, skill=skill) if skill else self.candidates_for_role(task.role)
            order = [n for n in ("claude_code", "codex", "antigravity", "localai_ollama") if n in eligible]
            return [AgentScore(n, 1.0 - i / 10) for i, n in enumerate(order)]

    r = submit(bridge, safe_env())
    bridge.process_queue(lambda name, cwd=None: adapters[name], router=Pinned(state=store))
    from solomon.remote import worker as wk
    final = wk.checkpoints_for(store, r.request_id)[-1]["data"]
    assert adapters["localai_ollama"].calls == 0
    assert final["attempts"] == ["claude_code", "codex"]
    assert final["substituted_from"] == "claude_code" and final["substituted_to"] == "codex"

def test_local_only_constraint_excludes_cloud_providers(world):
    store, bridge, worker, adapters = world
    worker.start()
    env = safe_env("owner", "p1", SAFE, role="knowledge_curator", constraints={"locality": "local"})
    submit(bridge, env)
    run_queue(bridge, adapters)
    assert adapters["localai_ollama"].calls == 1
    assert all(adapters[n].calls == 0 for n in ("claude_code", "codex", "antigravity"))


# --- continuity / revocation -------------------------------------------------------------

def test_switching_surfaces_preserves_durable_state(world):
    store, bridge, worker, adapters = world
    env = safe_env("owner", "p1", SAFE, surface="chatgpt", device_class="mobile")
    r = submit(bridge, env)
    q1 = {"identity_ref": "owner", "correlation_id": env["correlation_id"]}
    from_mobile = bridge.status(q1, sign(q1, KEY))
    fresh_bridge = TaskBridge(store, bridge.identities, Worker(store))  # desktop, new process
    from_desktop = fresh_bridge.status(q1, sign(q1, KEY))
    assert from_mobile["request_id"] == from_desktop["request_id"] == r.request_id
    assert from_mobile["status"] == from_desktop["status"]
    # continuation from another surface must stay in the same project
    cont = safe_env("owner", "p1", SAFE, surface="claude_desktop", device_class="desktop",
                        continuation={"task_id": r.task_id})
    assert submit(bridge, cont).status in (RemoteStatus.QUEUED, RemoteStatus.OFFLINE)
    bad = safe_env("owner", "p1", SAFE, continuation={"task_id": "task-nope"})
    assert submit(bridge, bad).status == RemoteStatus.REJECTED


def test_status_requires_auth_and_scope(world):
    _, bridge, _, _ = world
    r = submit(bridge, safe_env())
    q = {"identity_ref": "owner", "request_id": r.request_id}
    assert bridge.status(q, "bad")["status"] == "rejected"


def test_revocation_blocks_new_and_queued_work(world):
    store, bridge, worker, adapters = world
    queued = submit(bridge, safe_env())
    assert bridge.revoke("owner") == 1
    assert submit(bridge, safe_env()).status == RemoteStatus.REJECTED
    worker.start()
    assert run_queue(bridge, adapters) == []
    q = {"identity_ref": "viewer", "request_id": queued.request_id}
    assert bridge.status(q, sign(q, OTHER_KEY))["status"] == "blocked"
    assert sum(a.calls for a in adapters.values()) == 0


def test_bridge_module_opens_no_network_listener():
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "solomon" / "remote"
    text = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    for banned in ("socket", "http.server", "socketserver", "asyncio.start_server", "uvicorn", "flask"):
        assert banned not in text
