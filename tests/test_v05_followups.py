"""v0.5 follow-ups (D53-D55): remaining identifier migration with legacy
compatibility, remote requests executed via the CLI keep a truthful
status, v0.5 dashboard sections, read-only remote status, Skill Pack
install/uninstall."""

import json
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from solomon import cli, state as state_mod
from solomon.adapters import registry as adapter_registry
from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.descriptors import SkillScope
from solomon.gateway import GatewayMode, InvocationEnvelope, should_delegate
from solomon.result import TaskResult
from solomon.skills import PackInstallError, SkillRegistry, install_pack, uninstall_pack
from solomon.state import StateStore


# --- identifiers / compatibility ------------------------------------------------------

def test_gateway_force_mode_canonical_and_legacy():
    assert GatewayMode("FORCE_SOLOMON") is GatewayMode.FORCE_OCTAVRYN
    assert GatewayMode.FORCE_SOLOMON is GatewayMode.FORCE_OCTAVRYN
    d = should_delegate(InvocationEnvelope(request="x", caller_type="t"), GatewayMode("FORCE_SOLOMON"), project_id="p")
    assert d.delegate and d.reason == "FORCE_OCTAVRYN mode"


def test_gateway_cli_accepts_both_mode_spellings(capsys):
    for mode in ("FORCE_OCTAVRYN", "FORCE_SOLOMON"):
        assert cli.main(["gateway-evaluate", "--request", "x", "--mode", mode]) == 0
    capsys.readouterr()


def test_task_schema_title_and_cli_default_prog():
    schema = json.loads((ROOT / "04_Config_Schemas" / "task.schema.json").read_text(encoding="utf-8"))
    assert schema["title"] == "Octavryn Task"
    with pytest.raises(SystemExit):
        cli.main(["--help"])


def test_dashboard_title_keeps_legacy_name_for_readers():
    from solomon.dashboard import render_dashboard
    import inspect
    assert "OCTAVRYN SI -- DASHBOARD (formerly SOLOMON AI ORCHESTRATOR)" in inspect.getsource(render_dashboard)


# --- remote request executed via CLI execute-approved ------------------------------------

class Ok(AgentAdapter):
    def __init__(self, name):
        self.name = name

    def health(self):
        return AdapterHealth(True)

    def execute(self, task, prompt, timeout_s=600):
        now = self._now()
        return TaskResult(task_id=task.task_id, status="RESULT_RECEIVED", summary="ok", agent=self.name,
                          started_at=now, finished_at=now)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_DEFAULT_DB_PATH", tmp_path / "f.sqlite3")
    monkeypatch.setattr(cli, "get_gpu_telemetry", lambda: None)
    for n in adapter_registry.known_adapter_names():
        adapter_registry.register_adapter(n, lambda cwd=None, n=n: Ok(n))
    yield StateStore(tmp_path / "f.sqlite3")
    for n in ("claude_code", "codex", "localai_ollama", "antigravity"):
        adapter_registry.unregister_adapter(n)


def test_remote_approval_executed_via_cli_updates_remote_status(env):
    from solomon.remote.bridge import Identity, IdentityStore, TaskBridge, approval_message, sign
    from solomon.remote.protocol import new_envelope
    from solomon.remote.worker import Worker, get_request

    key = b"k" * 32
    bridge = TaskBridge(env, IdentityStore([Identity("owner", key, {"p1"}, can_approve=True)]), Worker(env))
    e = new_envelope("owner", "p1", "delete the stale cache directory",
                     constraints={"destructive_actions": "approval_required", "autonomy": "bounded"})
    r = bridge.submit(e, sign(e, key))
    msg = approval_message("owner", r.approval_id, env)
    bridge.approve(msg, sign(msg, key))
    assert cli.main(["execute-approved", r.approval_id]) == 0
    assert get_request(env, r.request_id)["status"] in ("completed", "verified")
    # the bridge worker will not run it a second time
    bridge.worker.start()
    assert bridge.process_queue(adapter_registry.load_adapter) == []


# --- dashboard sections / remote status CLI -----------------------------------------------

def test_v05_sections_render_and_degrade(env):
    from solomon.v05_status import render_v05_sections
    text = render_v05_sections(env)
    for label in ("Intelligences", "Surfaces", "Remote queue", "State layout"):
        assert label in text


def test_remote_status_cli(env, capsys):
    assert cli.main(["remote", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["requests_by_status"] == {}
    assert cli.main(["remote", "status", "--request-id", "nope"]) == 1


# --- skill pack install / uninstall ---------------------------------------------------------

PACK = """schema_version: "0.5"
id: demo_pack
version: {v}
name: Demo
skills:
  - {{id: demo.skill, version: 1, name: D, requires: {{capabilities: [coding]}}, permissions: [git_push]}}
"""


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_install_validates_copies_and_grants_nothing(tmp_path):
    src = write(tmp_path / "in" / "pack.yaml", PACK.format(v=1))
    target = tmp_path / "user_skills"
    out = install_pack(src, target, SkillScope.USER)
    assert out["requested_permissions"] == ["git_push"] and "No permission" in out["note"]
    reg = SkillRegistry()
    reg.load_dir(target, SkillScope.USER)
    skill = reg.get("demo.skill")
    assert skill is not None
    assert SkillRegistry.effective_permissions(skill) == ([], ["git_push"])


def test_reinstall_requires_replace_and_higher_version(tmp_path):
    target = tmp_path / "t"
    install_pack(write(tmp_path / "a.yaml", PACK.format(v=1)), target, SkillScope.USER)
    with pytest.raises(PackInstallError, match="already installed"):
        install_pack(write(tmp_path / "b.yaml", PACK.format(v=2)), target, SkillScope.USER)
    with pytest.raises(PackInstallError, match=">="):
        install_pack(write(tmp_path / "c.yaml", PACK.format(v=1)), target, SkillScope.USER, replace=True)
    assert install_pack(tmp_path / "b.yaml", target, SkillScope.USER, replace=True)["version"] == 2


def test_install_refuses_clashing_skill_ids_and_invalid_packs(tmp_path):
    target = tmp_path / "t"
    install_pack(write(tmp_path / "a.yaml", PACK.format(v=1)), target, SkillScope.USER)
    other = PACK.format(v=1).replace("id: demo_pack", "id: other_pack")
    with pytest.raises(PackInstallError, match="already provided"):
        install_pack(write(tmp_path / "o.yaml", other), target, SkillScope.USER)
    with pytest.raises(PackInstallError, match="invalid"):
        install_pack(write(tmp_path / "bad.yaml", 'schema_version: "0.4"\nid: x\n'), target, SkillScope.USER)


def test_uninstall_moves_aside_and_is_not_loaded(tmp_path):
    target = tmp_path / "t"
    install_pack(write(tmp_path / "a.yaml", PACK.format(v=1)), target, SkillScope.USER)
    out = uninstall_pack("demo_pack", target)
    assert pathlib.Path(out["moved_to"]).exists()
    reg = SkillRegistry()
    reg.load_dir(target, SkillScope.USER)
    assert reg.get("demo.skill") is None
    with pytest.raises(PackInstallError):
        uninstall_pack("demo_pack", target)


def test_skills_install_cli_user_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OCTAVRYN_USER_HOME", str(tmp_path / "home"))
    src = write(tmp_path / "p.yaml", PACK.format(v=1))
    assert cli.main(["skills", "install", str(src)]) == 0
    assert "demo_pack" in capsys.readouterr().out
    assert cli.main(["skills", "uninstall", "demo_pack"]) == 0


# --- diagnostics v0.5 section ---------------------------------------------------------------

def test_diagnostics_v05_section_is_privacy_preserving_by_default(env):
    from solomon.diagnostics import build_diagnostics_report
    from solomon.registry import ProjectRegistry

    report = build_diagnostics_report(env, ProjectRegistry(), {}).to_dict()
    v = report["octavryn_v05"]
    assert v["version"] == "0.5.0-dev"
    assert set(v) >= {"state_layout", "intelligences", "surfaces", "remote_queue", "skills"}
    assert "active_db" not in v["state_layout"]
    sensitive = build_diagnostics_report(env, ProjectRegistry(), {}, include_sensitive=True).to_dict()
    assert "active_db" in sensitive["octavryn_v05"]["state_layout"]


# --- documentation consistency ---------------------------------------------------------------

def test_documented_octavryn_commands_exist():
    import re

    # Every `octavryn <cmd>` in the docs must be accepted by the real parser.
    docs = [ROOT / "README.md", ROOT / "examples" / "basic_walkthrough.md", ROOT / "examples" / "README.md",
            ROOT / "hooks" / "README.md", ROOT / "addons" / "README.md"]
    found = set()
    for d in docs:
        found |= set(re.findall(r"(?:python -m octavryn|`octavryn) ([a-z][a-z0-9-]+)", d.read_text(encoding="utf-8")))
    assert found, "no documented commands found"
    for cmd in sorted(found):
        with pytest.raises(SystemExit) as exc:
            cli.main([cmd, "--help"])
        assert exc.value.code == 0, f"documented command '{cmd}' does not exist"
