"""D66-D69 (2026-09-25 environment integration): project cwd for CLI
adapters, per-project least-privilege policy, project skills at routing
and execution, the Codex / ChatGPT Desktop MCP entry, surface detection
and MCP discovery across scopes."""

import json
import pathlib
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon import codex_mcp, surfaces
from solomon.adapters import registry as adapter_registry
from solomon.adapters.antigravity_adapter import AntigravityAdapter
from solomon.adapters.codex_adapter import CodexAdapter
from solomon.descriptors import SkillDescriptor, SkillScope
from solomon.governance import evaluate
from solomon.intelligence_registry import discover_codex_mcp_servers, discover_mcp_servers
from solomon.models import Risk, Task
from solomon.project_policy import PolicyError, load_policies
from solomon.router import Router
from solomon.state import StateStore

POLICY = r"""
projects:
  restricted:
    allowed_skills: [lab.backtest, lab.status]
    denied_capabilities: [broker_execution, fund_transfer, live_trading, credential_management]
    denied_permissions: [orders.submit, broker.credentials]
    skill_grants: {lab.backtest: [fs.write.runs]}
    forbidden_prompt_patterns: ['\blive[- ]?trad', 'submit (an? )?orders?']
    max_autonomy: 2
    allowed_adapters: [claude_code, codex]
    execution_profile:
      claude_code: {permission_mode: default, allowed_tools: [Read, Grep], disallowed_tools: [WebFetch]}
      codex: {sandbox: read-only}
"""


def skill(sid, caps=("backtesting",), perms=(), risk="NORMAL"):
    return SkillDescriptor(id=sid, version=1, name=sid, risk=risk, required_capabilities=list(caps),
                           requested_permissions=list(perms), scope=SkillScope.PROJECT)


def task(project="restricted", autonomy=None):
    return Task(goal_id="g", project_id=project, type="adhoc", role="researcher",
                definition_of_done=["result_recorded"], risk=Risk.LOW, autonomy=autonomy)


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    p = tmp_path / "project_policies.yaml"
    p.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("OCTAVRYN_PROJECT_POLICIES", str(p))
    return p


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "s.sqlite3")


# -- policy file --------------------------------------------------------

def test_policy_loads_and_missing_file_is_unrestricted(policy_file, tmp_path):
    pols = load_policies(policy_file)
    assert pols["restricted"].allowed_skills == ["lab.backtest", "lab.status"]
    assert load_policies(tmp_path / "absent.yaml") == {}


@pytest.mark.parametrize("body", [
    "projects: {x: {unknown_key: 1}}",
    "projects: {x: {denied_permissions: [a], skill_grants: {s: [a]}}}",
    "projects: {x: {execution_profile: {codex: {sandbox: danger-full-access}}}}",
    "projects: {x: {execution_profile: {antigravity: {sandbox: false}}}}",
    "projects: {x: {execution_profile: {localai_ollama: {model: y}}}}",
    "projects: {x: {forbidden_prompt_patterns: ['(']}}",
])
def test_invalid_policies_are_rejected(tmp_path, body):
    p = tmp_path / "p.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policies(p)


# -- governance gate ------------------------------------------------------

def gate(decision):
    return next(g for g in decision.gates if g.gate == "project_policy")


def test_allowed_skill_passes_and_grants_apply(policy_file, store):
    d = evaluate(task(), "run the backtest", store, skill=skill("lab.backtest", perms=["fs.write.runs"]))
    assert d.allowed, d.reason
    assert gate(d).outcome == "pass"


def test_role_default_is_denied_when_skills_are_allowlisted(policy_file, store):
    d = evaluate(task(), "run the backtest", store)
    assert d.denied and "explicit skill" in d.reason


def test_skill_outside_allowlist_is_denied(policy_file, store):
    assert evaluate(task(), "x", store, skill=skill("lab.other")).denied


def test_denied_capability_is_denied_even_when_allowlisted(policy_file, store):
    d = evaluate(task(), "x", store, skill=skill("lab.status", caps=["order_submission"]))
    assert d.denied and "broker_execution" in d.reason  # alias normalized


def test_denied_permission_is_denied(policy_file, store):
    d = evaluate(task(), "x", store, skill=skill("lab.status", perms=["orders.submit"]))
    assert d.denied and "orders.submit" in d.reason


@pytest.mark.parametrize("prompt", ["enable LIVE trading now", "submit an order for 7203"])
def test_forbidden_prompt_is_denied_not_sent_to_approval(policy_file, store, prompt):
    d = evaluate(task(), prompt, store, skill=skill("lab.backtest"))
    assert d.denied and not d.requires_approval


def test_max_autonomy_clamps_the_dial(policy_file, store):
    t = task(autonomy=5)
    evaluate(t, "x", store, skill=skill("lab.status"))
    assert t.autonomy == 2


def test_other_projects_are_unaffected(policy_file, store):
    d = evaluate(task(project="free"), "x", store)
    assert d.allowed and not any(g.gate == "project_policy" for g in d.gates)


def test_invalid_policy_file_fails_closed(tmp_path, monkeypatch, store):
    p = tmp_path / "bad.yaml"
    p.write_text("projects: [not, a, mapping]", encoding="utf-8")
    monkeypatch.setenv("OCTAVRYN_PROJECT_POLICIES", str(p))
    assert evaluate(task(project="free"), "x", store).denied


# -- adapters for a project -------------------------------------------------

def test_cli_adapters_run_in_the_project_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter_registry, "project_repo_path", lambda pid: str(tmp_path) if pid == "p" else None)
    for name in ("claude_code", "codex", "antigravity"):
        assert adapter_registry.load_for_project(name, "p").cwd == str(tmp_path)
    assert adapter_registry.load_for_project("claude_code", "p", cwd="X").cwd == "X"  # worktree wins


def test_policy_adapter_allowlist_and_profiles(policy_file, monkeypatch):
    monkeypatch.setattr(adapter_registry, "project_repo_path", lambda pid: None)
    agy = adapter_registry.load_for_project("antigravity", "restricted")
    assert not agy.health().available and "not allowed" in agy.health().detail
    claude = adapter_registry.load_for_project("claude_code", "restricted")
    cmd = claude.build_command("hi")
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Grep"
    assert cmd[cmd.index("--disallowedTools") + 1] == "WebFetch"
    assert adapter_registry.load_for_project("codex", "restricted").sandbox == "read-only"


def test_unrestricted_project_keeps_default_commands(monkeypatch):
    monkeypatch.setattr(adapter_registry, "project_repo_path", lambda pid: None)
    cmd = adapter_registry.load_for_project("claude_code", "free").build_command("hi")
    assert "--allowedTools" not in cmd and "--permission-mode" not in cmd
    a = AntigravityAdapter()
    a.apply_profile({"mode": "plan"})
    assert a.sandbox and a.mode == "plan"
    c = CodexAdapter()
    c.apply_profile({"sandbox": "danger-full-access"})
    assert c.sandbox == "workspace-write"


# -- project skills resolve at routing time ----------------------------------

def test_router_loads_project_skills(tmp_path):
    d = tmp_path / ".octavryn" / "skills"
    d.mkdir(parents=True)
    (d / "lab.yaml").write_text(
        'schema_version: "0.5"\nid: lab\nversion: 1\nname: Lab\nskills:\n'
        '  - {id: lab.backtest, version: 1, name: B, requires: {capabilities: [backtesting]}}\n',
        encoding="utf-8")
    assert Router().skill_for_task(task(), "lab.backtest") is None
    r = Router(project_repo_path=str(tmp_path))
    s = r.skill_for_task(task(), "lab.backtest")
    assert s is not None and s.scope == SkillScope.PROJECT
    cands = [c.intelligence_id for c in r.resolve_candidates(task(), skill=s) if c.eligible]
    assert "claude_code" in cands and "codex" in cands and "localai_ollama" not in cands


# -- Codex / ChatGPT Desktop MCP entry -----------------------------------------

EXISTING = """notify = ["x"]

[mcp_servers.memory]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-memory"]

[windows]
sandbox = "elevated"
"""


def test_codex_mcp_apply_remove_roundtrip(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(EXISTING, encoding="utf-8", newline="")
    block = codex_mcp.planned_block(python="C:\\py\\python.exe", src_dir=pathlib.Path("C:\\o\\src"))
    out = codex_mcp.apply(cfg, block)
    assert out["action"] == "added"
    assert pathlib.Path(out["backup"]).read_text(encoding="utf-8") == EXISTING
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    ours = data["mcp_servers"]["octavryn"]
    assert ours["enabled_tools"] == surfaces.READ_ONLY_MCP_TOOLS
    assert ours["command"] == "C:\\py\\python.exe" and ours["env"]["PYTHONPATH"] == "C:\\o\\src"
    assert data["windows"] == {"sandbox": "elevated"} and "memory" in data["mcp_servers"]
    assert codex_mcp.apply(cfg)["action"] == "none"
    assert codex_mcp.status(cfg)["octavryn_registered"]
    assert codex_mcp.remove(cfg)["action"] == "removed"
    assert cfg.read_text(encoding="utf-8") == EXISTING


def test_codex_mcp_does_not_remove_an_unmanaged_entry(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(EXISTING + '\n[mcp_servers.octavryn]\ncommand = "x"\n', encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    assert codex_mcp.remove(cfg)["action"] == "none"
    assert cfg.read_text(encoding="utf-8") == before


def test_codex_mcp_refuses_invalid_existing_toml(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        codex_mcp.apply(cfg)
    assert cfg.read_text(encoding="utf-8") == "this is = = not toml"


# -- surfaces / discovery -------------------------------------------------------

def test_chatgpt_desktop_detected_from_codex_package(tmp_path):
    (tmp_path / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0").mkdir(parents=True)
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "config.toml").write_text(EXISTING, encoding="utf-8")
    env = {"LOCALAPPDATA": str(tmp_path), "CODEX_HOME": str(home)}
    d = surfaces.detect_chatgpt_desktop(env)
    assert d.availability.value == "available" and d.bridge == "mcp"
    assert d.octavryn_capabilities_offered == [] and d.privileged_execution is False
    codex_mcp.apply(home / "config.toml")
    assert surfaces.detect_chatgpt_desktop(env).octavryn_capabilities_offered == surfaces.READ_ONLY_MCP_TOOLS


def test_chatgpt_desktop_absent(tmp_path):
    d = surfaces.detect_chatgpt_desktop({"LOCALAPPDATA": str(tmp_path), "CODEX_HOME": str(tmp_path)})
    assert d.availability.value == "absent"


def test_mcp_discovery_reads_local_scope_and_codex(tmp_path):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"a": {"env": {"KEY": "secret"}}},
                               "projects": {"C:/x": {"mcpServers": {"octavryn": {}}}}}), encoding="utf-8")
    tools = discover_mcp_servers(cfg)
    assert [t.id for t in tools] == ["mcp:a", "mcp:octavryn"]
    assert "secret" not in json.dumps([t.to_dict() for t in tools])
    toml = tmp_path / "config.toml"
    toml.write_text(EXISTING, encoding="utf-8")
    assert [t.id for t in discover_codex_mcp_servers(toml)] == ["codex-mcp:memory"]


def test_cli_governance_check_and_route_see_project_skills(tmp_path, monkeypatch, capsys, policy_file):
    from solomon import cli
    from solomon import state as state_mod

    d = tmp_path / "repo" / ".octavryn" / "skills"
    d.mkdir(parents=True)
    (d / "lab.yaml").write_text(
        'schema_version: "0.5"\nid: lab\nversion: 1\nname: Lab\nskills:\n'
        '  - {id: lab.status, version: 1, name: S, risk: LOW, requires: {capabilities: [status_reporting]}}\n',
        encoding="utf-8")
    monkeypatch.setattr(state_mod, "_DEFAULT_DB_PATH", tmp_path / "cli.sqlite3")
    monkeypatch.setattr(cli, "get_gpu_telemetry", lambda: None)
    monkeypatch.setattr(adapter_registry, "project_repo_path",
                        lambda pid: str(tmp_path / "repo") if pid == "restricted" else None)
    assert cli.main(["governance-check", "--project-id", "restricted", "--role", "researcher",
                     "--skill", "lab.status", "--prompt", "report status"]) == 0
    assert cli.main(["governance-check", "--project-id", "restricted", "--role", "researcher",
                     "--skill", "lab.status", "--prompt", "submit an order"]) == 2
    capsys.readouterr()
    assert cli.main(["route", "--project-id", "restricted", "--role", "researcher", "--skill", "lab.status"]) == 0
    out = capsys.readouterr().out
    assert "excluded\tlocalai_ollama: missing capability status_reporting" in out
    assert "claude_code\ttotal=" in out


def test_no_available_adapter_blocks_the_task_instead_of_leaving_it_queued(store):
    from solomon.adapters.base import AdapterHealth, AgentAdapter
    from solomon.execution import execute_with_fallback
    from solomon.models import TaskStatus

    class Down(AgentAdapter):
        def __init__(self, name):
            self.name = name

        def health(self):
            return AdapterHealth(False, "not found on PATH")

        def execute(self, task, prompt, timeout_s=600):  # pragma: no cover - never called
            raise AssertionError("unavailable adapter executed")

    t = Task(goal_id="g", project_id="free", type="adhoc", role="coder", definition_of_done=["result_recorded"])
    store.save_task(t)
    out = execute_with_fallback(t, "x", Router(state=store), lambda n: Down(n), state=store, authorized=True)
    assert out.final_result is None and all(a.skipped for a in out.attempts)
    assert store.get_task(t.task_id)["status"] == TaskStatus.BLOCKED.value


def test_execute_approved_rechecks_project_policy_before_consuming(tmp_path, monkeypatch, policy_file):
    """D73: an approval created before a policy was tightened must not run."""
    from solomon import cli
    from solomon import state as state_mod
    from solomon.governance import GovernanceDecision, request_approval

    monkeypatch.setattr(state_mod, "_DEFAULT_DB_PATH", tmp_path / "cli.sqlite3")
    monkeypatch.setattr(adapter_registry, "project_repo_path", lambda pid: None)
    st = StateStore(tmp_path / "cli.sqlite3")
    t = task()
    st.save_task(t)
    rid = request_approval(st, t, GovernanceDecision(risk=Risk.LOW),
                           {"kind": "route", "prompt": "summarize", "timeout": 5, "max_attempts": 1,
                            "skill_id": None})
    st.decide_approval_request(rid, approved=True, note="test", decided_by="tester")
    assert cli.main(["execute-approved", rid]) == 1  # role default is denied by the policy
    assert st.get_approval_request(rid)["status"] == "approved"  # not consumed


def test_desktop_mcp_apply_refuses_while_desktop_runs(tmp_path, monkeypatch, capsys):
    """D74: Claude Desktop rewrote its config from memory and dropped an
    entry written while it ran (observed 2026-09-26)."""
    from solomon import cli, desktop_mcp

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('{"preferences": {}}', encoding="utf-8")
    monkeypatch.setattr(desktop_mcp, "desktop_running", lambda: True)
    assert cli.main(["desktop-mcp", "apply", "--config", str(cfg)]) == 3
    assert "octavryn" not in cfg.read_text(encoding="utf-8")
    assert cli.main(["desktop-mcp", "apply", "--config", str(cfg), "--allow-running"]) == 0
    assert "octavryn" in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
    monkeypatch.setattr(desktop_mcp, "desktop_running", lambda: False)
    assert cli.main(["desktop-mcp", "remove", "--config", str(cfg)]) == 0


def test_codex_replace_retries_transient_permission_error(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = codex_mcp.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr(codex_mcp.os, "replace", flaky)
    cfg = tmp_path / "config.toml"
    cfg.write_text(EXISTING, encoding="utf-8")
    assert codex_mcp.apply(cfg)["action"] == "added" and calls["n"] == 3


AUTH_FAIL = "\n".join([
    '{"type":"thread.started","thread_id":"t"}',
    '{"type":"turn.started"}',
    '{"type":"error","message":"Reconnecting... 1/5 (unexpected status 401 Unauthorized: Incorrect API key provided: sk-svcac*****fvMA.)"}',
    '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Incorrect API key provided: sk-svcac******************fvMA. url: https://chatgpt.com/backend-api/codex/responses"}}',
])


def test_codex_turn_failed_is_failed_redacted_and_retryable(monkeypatch, tmp_path):
    """D76 (seen 2026-09-26: Codex CLI credential rejected with 401)."""
    import subprocess as sp

    from solomon.adapters import codex_adapter
    from solomon.execution import _is_retryable_failure

    class P:
        returncode, stdout, stderr = 1, AUTH_FAIL, "rmcp noise"

    monkeypatch.setattr(codex_adapter.subprocess, "run", lambda *a, **k: P())
    monkeypatch.setattr(CodexAdapter, "_project_repo_path", staticmethod(lambda pid: None))
    r = CodexAdapter(cwd=str(tmp_path)).execute(task(project="free"), "x")
    assert r.status == "FAILED"
    assert r.summary.startswith("codex turn failed: unexpected status 401")
    assert "svcac" not in r.summary and "fvMA" not in r.summary
    assert r.uncertainties[0].startswith("unavailable:")
    from solomon.adapters.base import AdapterHealth
    assert _is_retryable_failure(r, AdapterHealth(True, "ok"))


def test_codex_success_without_turn_failure_is_unchanged(monkeypatch, tmp_path):
    from solomon.adapters import codex_adapter

    ok = '{"type":"item.completed","item":{"type":"agent_message","text":"PONG"}}\n{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'

    class P:
        returncode, stdout, stderr = 0, ok, ""

    monkeypatch.setattr(codex_adapter.subprocess, "run", lambda *a, **k: P())
    monkeypatch.setattr(CodexAdapter, "_project_repo_path", staticmethod(lambda pid: None))
    r = CodexAdapter(cwd=str(tmp_path)).execute(task(project="free"), "x")
    assert r.status == "RESULT_RECEIVED" and r.summary == "PONG" and r.uncertainties == []


def test_real_failure_followed_by_skip_stays_failed_d77(store):
    from solomon.adapters.base import AdapterHealth, AgentAdapter
    from solomon.execution import execute_with_fallback
    from solomon.models import TaskStatus
    from solomon.result import TaskResult

    class TimesOut(AgentAdapter):
        name = "claude_code"

        def health(self):
            return AdapterHealth(True, "ok")

        def execute(self, task, prompt, timeout_s=600):
            now = self._now()
            return TaskResult(task_id=task.task_id, status="FAILED", summary="timed out", agent=self.name,
                              started_at=now, finished_at=now, uncertainties=["timeout"])

    class Down(AgentAdapter):
        def __init__(self, name):
            self.name = name

        def health(self):
            return AdapterHealth(False, "not found on PATH")

        def execute(self, task, prompt, timeout_s=600):  # pragma: no cover
            raise AssertionError

    t = Task(goal_id="g", project_id="free", type="adhoc", role="researcher", definition_of_done=["result_recorded"])
    store.save_task(t)
    router = Router(state=store)
    out = execute_with_fallback(t, "x", router, lambda n: TimesOut() if n == "claude_code" else Down(n),
                                state=store, authorized=True, max_attempts=2)
    assert out.final_result is not None and out.final_result.status == "FAILED"
    assert store.get_task(t.task_id)["status"] == TaskStatus.FAILED.value
