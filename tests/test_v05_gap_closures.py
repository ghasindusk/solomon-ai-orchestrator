"""v0.5 gap-audit closures (D58-D61): evaluation records (spec 09 gate 9),
approval context (spec 07), remote status fields (spec 15), richer
checkpoints (spec 16), remote kill switch (spec 17)."""

import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon import cli, state as state_mod
from solomon.adapters import registry as adapter_registry
from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.evaluation import outcome_for
from solomon.governance import authorize_execution, evaluate, request_approval
from solomon.models import Task
from solomon.policy import PolicyEngine
from solomon.remote.bridge import Identity, IdentityStore, TaskBridge, approval_message, sign
from solomon.remote.protocol import RemoteStatus, new_envelope
from solomon.remote.worker import Worker, checkpoints_for
from solomon.result import TaskResult, Usage
from solomon.state import StateStore


class Fake(AgentAdapter):
    def __init__(self, name, ok=True, tokens=True):
        self.name, self.ok, self.tokens = name, ok, tokens

    def health(self):
        return AdapterHealth(True)

    def execute(self, task, prompt, timeout_s=600):
        now = self._now()
        u = Usage(input_tokens=3, output_tokens=4) if self.tokens else Usage()
        return TaskResult(task_id=task.task_id, status="RESULT_RECEIVED" if self.ok else "FAILED", summary="x",
                          agent=self.name, started_at=now, finished_at=now, usage=u,
                          uncertainties=[] if self.ok else ["timeout"], tests=["pytest: 3 passed"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_DEFAULT_DB_PATH", tmp_path / "g.sqlite3")
    monkeypatch.setattr(cli, "get_gpu_telemetry", lambda: None)
    for n in adapter_registry.known_adapter_names():
        adapter_registry.register_adapter(n, lambda cwd=None, n=n: Fake(n))
    yield StateStore(tmp_path / "g.sqlite3")
    for n in ("claude_code", "codex", "localai_ollama", "antigravity"):
        adapter_registry.unregister_adapter(n)


# --- evaluation -------------------------------------------------------------------------

def test_outcome_classification():
    assert outcome_for(None, None) == "unknown"
    assert outcome_for("FAILED", "FAILED") == "failure"
    assert outcome_for("RESULT_RECEIVED", "RESULT_RECEIVED") == "success"
    assert outcome_for("RESULT_RECEIVED", "COMPLETE") == "verified_success"


def test_every_execution_path_records_an_evaluation(env, capsys):
    assert cli.main(["route-and-run", "--role", "coder", "--project-id", "p1", "--prompt", "summarize"]) == 0
    assert cli.main(["run-task", "--adapter", "codex", "--project-id", "p1", "--prompt", "summarize"]) == 0
    evs = env.list_evaluations()
    assert len(evs) == 2
    assert all(e["tokens"] == 7 and e["outcome"] in ("success", "verified_success") for e in evs)
    capsys.readouterr()
    assert cli.main(["evaluations"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 2


def test_fallback_evaluation_records_participants_and_retries(env):
    adapter_registry.register_adapter("claude_code", lambda cwd=None: Fake("claude_code", ok=False))
    adapter_registry.register_adapter("antigravity", lambda cwd=None: Fake("antigravity", ok=False))
    cli.main(["route-and-run", "--role", "coder", "--project-id", "p1", "--prompt", "summarize",
              "--max-attempts", "3"])
    [e] = env.list_evaluations()
    assert len(e["participants"]) >= 2 and e["retries"] == len(e["participants"]) - 1


def test_unknown_tokens_stay_unknown(env):
    adapter_registry.register_adapter("codex", lambda cwd=None: Fake("codex", tokens=False))
    cli.main(["run-task", "--adapter", "codex", "--project-id", "p1", "--prompt", "summarize"])
    assert env.list_evaluations()[-1]["tokens"] is None


# --- approval context --------------------------------------------------------------------

def test_approval_context_is_complete_and_outside_the_hash(env, capsys):
    t = Task(goal_id="g", project_id="p1", type="adhoc", role="coder", definition_of_done=["result_recorded"])
    env.save_task(t)
    d = evaluate(t, "force push the release branch", env, PolicyEngine())
    rid = request_approval(env, t, d, {"kind": "route", "prompt": "force push the release branch"})
    ctx = env.get_approval_request(rid)["task_params"]["approval_context"]
    assert set(ctx) == {"action", "reason", "risk", "affected_resources", "proposed_participant",
                        "reversibility", "budget_impact", "alternatives"}
    assert "irreversible" in ctx["reversibility"] and ctx["affected_resources"][0] == "project:p1"
    assert "routed by capability" in ctx["proposed_participant"]
    env.decide_approval_request(rid, approved=True, decided_by="human:t")
    assert authorize_execution(env, rid).ok  # the context does not affect the binding
    capsys.readouterr()
    cli.main(["approvals", "list", "--status", "executed"])
    assert "reversibility:" in capsys.readouterr().out


# --- remote: status fields, checkpoints, kill ---------------------------------------------

KEY = b"k" * 32


def bridge(env):
    return TaskBridge(env, IdentityStore([Identity("owner", KEY, {"p1"}, can_approve=True)]), Worker(env))


def submit(b, goal="summarize notes", **c):
    c.setdefault("autonomy", "bounded")
    e = new_envelope("owner", "p1", goal, constraints=c)
    return e, b.submit(e, sign(e, KEY))


def test_status_exposes_spec15_fields_and_unknowns(env):
    b = bridge(env)
    _, r = submit(b)
    q = {"identity_ref": "owner", "request_id": r.request_id}
    st = b.status(q, sign(q, KEY))
    assert st["phase"] == "offline"
    assert st["completion_pct"] == 0.0
    assert st["available_participants"] is None  # registry never refreshed: UNKNOWN, not []
    assert st["latest_tests"] is None and st["kill_switch_engaged"] is False
    b.worker.start()
    b.process_queue(adapter_registry.load_adapter)
    st = b.status(q, sign(q, KEY))
    assert st["latest_tests"] == ["pytest: 3 passed"]


def test_checkpoints_carry_spec16_fields(env):
    b = bridge(env)
    _, r = submit(b)
    b.worker.start()
    b.process_queue(adapter_registry.load_adapter)
    cps = checkpoints_for(env, r.request_id)
    first, last = cps[0]["data"], cps[-1]["data"]
    assert {"pending_work", "affected_resources", "skill", "verification", "rollback"} <= set(first)
    assert {"participants", "affected_resources", "rollback", "verification"} <= set(last)


def test_kill_switch_blocks_everything_and_persists(env):
    b = bridge(env)
    submit(b)
    _, waiting = submit(b, goal="delete the stale cache directory", destructive_actions="approval_required")
    assert b.kill("incident", "human:owner") == 2
    assert submit(b)[1].status == RemoteStatus.REJECTED
    msg = approval_message("owner", waiting.approval_id, env)
    assert b.approve(msg, sign(msg, KEY)).status == RemoteStatus.REJECTED
    b.worker.start()
    assert b.process_queue(adapter_registry.load_adapter) == []
    assert bridge(env).killed  # persisted: a new bridge instance is still killed
    with pytest.raises(ValueError):
        b.release_kill("", "")
    b.release_kill("resolved", "human:owner")
    assert submit(b)[1].status in (RemoteStatus.QUEUED, RemoteStatus.OFFLINE)


def test_remote_kill_cli(env, capsys):
    assert cli.main(["remote", "kill", "engage", "--reason", "test", "--by", "owner"]) == 0
    assert bridge(env).killed
    assert cli.main(["remote", "kill", "release", "--reason", "done", "--by", "owner"]) == 0
    assert not bridge(env).killed
