"""Octavryn SI v0.5 R5 (surfaces) and R7 (migration, canonical CLI,
compatibility alias). Spec 12 "Migration tests" + desktop-surface rows."""

import json
import os
import sqlite3
import subprocess
import sys
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import pytest

from solomon import migration
from solomon.descriptors import Availability
from solomon.state import StateStore
from solomon.surfaces import detect_chatgpt_desktop, detect_claude_desktop, detect_surfaces


# --- R5 surfaces -------------------------------------------------------------------

def test_no_surfaces_installed_is_a_valid_environment(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming")}
    surfaces = detect_surfaces(env)
    assert [s.availability for s in surfaces] == [Availability.ABSENT, Availability.ABSENT]
    assert all(s.privileged_execution is False for s in surfaces)


def test_claude_desktop_msix_with_our_mcp_registered(tmp_path):
    pkg = tmp_path / "local" / "Packages" / "Claude_abc123"
    cfg = pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"octavryn": {"command": "python", "env": {"TOKEN": "zzz"}}}}),
                   encoding="utf-8")
    s = detect_claude_desktop({"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "r")})
    assert s.availability == Availability.AVAILABLE
    assert s.bridge == "mcp"
    assert s.octavryn_capabilities_offered == ["list_projects", "get_dashboard", "get_usage", "list_approvals"]
    assert "zzz" not in json.dumps(s.to_dict())
    assert s.to_dict()["privileged_execution"] is False


def test_claude_desktop_without_our_mcp_is_offered_nothing(tmp_path):
    (tmp_path / "local" / "AnthropicClaude").mkdir(parents=True)
    cfg = tmp_path / "roaming" / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"mcpServers": {"other": {}}}', encoding="utf-8")
    s = detect_claude_desktop({"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming")})
    assert s.availability == Availability.AVAILABLE and s.octavryn_capabilities_offered == []


def test_chatgpt_desktop_present_bridge_unknown_not_guessed(tmp_path):
    (tmp_path / "local" / "Packages" / "OpenAI.ChatGPT-Desktop_x").mkdir(parents=True)
    # D69: no readable Codex config (the app's MCP bridge) -> bridge unknown
    s = detect_chatgpt_desktop({"LOCALAPPDATA": str(tmp_path / "local"), "CODEX_HOME": str(tmp_path / "none")})
    assert s.availability == Availability.AVAILABLE
    assert s.bridge is None
    assert s.octavryn_capabilities_offered == []


def test_surface_modules_never_import_gui_automation():
    text = (SRC / "solomon" / "surfaces.py").read_text(encoding="utf-8")
    for banned in ("pyautogui", "pywinauto", "subprocess", "win32gui"):
        assert banned not in text


# --- R7 migration --------------------------------------------------------------------

def _legacy_db(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_dir / migration.LEGACY_DB_NAME)
    store.create_approval_request("appr-1", "p", "HIGH", "r", {"x": 1})
    store.log_event(project_id="p", event="e")
    store.close()


def test_fresh_install_needs_no_migration(tmp_path):
    assert migration.apply(tmp_path)["action"] == "none"
    assert migration.resolve_default_db_path(tmp_path).name == migration.LEGACY_DB_NAME


def test_dry_run_changes_nothing(tmp_path):
    _legacy_db(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    plan = migration.apply(tmp_path, dry_run=True)
    assert plan["action"] == "migrate" and plan["dry_run"]
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_apply_backs_up_verifies_and_switches_default(tmp_path):
    _legacy_db(tmp_path)
    counts = migration.table_counts(tmp_path / migration.LEGACY_DB_NAME)
    rec = migration.apply(tmp_path)
    assert pathlib.Path(rec["backup"]).exists()
    assert migration.table_counts(pathlib.Path(rec["backup"])) == counts
    new_counts = migration.table_counts(tmp_path / migration.NEW_DB_NAME)
    for table, n in counts.items():
        assert new_counts[table] == n
    assert (tmp_path / migration.LEGACY_DB_NAME).exists()  # copy, never move
    assert migration.resolve_default_db_path(tmp_path).name == migration.NEW_DB_NAME
    store = StateStore(tmp_path / migration.NEW_DB_NAME)
    assert store.get_approval_request("appr-1")["task_params"] == {"x": 1}  # historical data readable


def test_apply_is_idempotent(tmp_path):
    _legacy_db(tmp_path)
    migration.apply(tmp_path)
    assert migration.apply(tmp_path)["action"] == "none"


def test_refuses_when_legacy_changed_after_migration(tmp_path):
    _legacy_db(tmp_path)
    migration.apply(tmp_path)
    s = StateStore(tmp_path / migration.LEGACY_DB_NAME)
    s.log_event(project_id="p", event="late write")
    s.close()
    with pytest.raises(migration.MigrationError):
        migration.apply(tmp_path)


def test_refuses_to_overwrite_unmarked_target(tmp_path):
    _legacy_db(tmp_path)
    (tmp_path / migration.NEW_DB_NAME).write_bytes(b"")
    with pytest.raises(migration.MigrationError):
        migration.apply(tmp_path)


def test_rollback_keeps_data_written_after_migration(tmp_path):
    _legacy_db(tmp_path)
    migration.apply(tmp_path)
    s = StateStore(tmp_path / migration.NEW_DB_NAME)
    s.create_approval_request("appr-after", "p", "LOW", "post-migration", {})
    s.close()
    out = migration.rollback(tmp_path)
    assert out["action"] == "rollback"
    assert migration.resolve_default_db_path(tmp_path).name == migration.LEGACY_DB_NAME
    legacy = StateStore(tmp_path / migration.LEGACY_DB_NAME)
    assert legacy.get_approval_request("appr-after") is not None
    assert pathlib.Path(out["octavryn_db_moved_to"]).exists()  # moved aside, not deleted
    assert migration.rollback(tmp_path)["action"] == "none"
    # and it can be migrated again afterwards
    legacy.close()
    assert migration.apply(tmp_path)["action"] == "migrate"


def test_octavryn_db_env_var_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTAVRYN_DB", str(tmp_path / "custom.db"))
    assert migration.resolve_default_db_path(tmp_path) == tmp_path / "custom.db"


def test_v04_database_gains_decided_by_column_without_losing_rows(tmp_path):
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE approval_requests (request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                 "risk TEXT NOT NULL, reason TEXT NOT NULL, task_params TEXT NOT NULL, status TEXT NOT NULL, "
                 "requested_at TEXT NOT NULL, decided_at TEXT, decision_note TEXT)")
    conn.execute("INSERT INTO approval_requests VALUES ('a','p','HIGH','r','{}','pending','t',NULL,NULL)")
    conn.commit()
    conn.close()
    s = StateStore(db)
    req = s.get_approval_request("a")
    assert req["decided_by"] is None and req["status"] == "pending"
    StateStore(db).close()  # second open: idempotent


# --- canonical CLI / alias --------------------------------------------------------------

def test_octavryn_namespace_aliases_same_module_objects():
    import octavryn.router
    import solomon.router
    import octavryn.state
    import solomon.state

    assert octavryn.router is solomon.router
    assert octavryn.state is solomon.state


def _run(module, *args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.pop("OCTAVRYN_SUPPRESS_DEPRECATION", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", module, *args], capture_output=True,
                          encoding="utf-8", errors="replace", env=env, timeout=120)


def test_octavryn_cli_is_canonical_without_notice():
    proc = _run("octavryn", "skills")
    assert proc.returncode == 0
    assert "role.coder" in proc.stdout
    assert "deprecated" not in proc.stderr


def test_solomon_alias_works_and_prints_notice_on_stderr_only():
    proc = _run("solomon.cli", "skills")
    assert proc.returncode == 0 and "role.coder" in proc.stdout
    assert "deprecated" in proc.stderr and "deprecated" not in proc.stdout
    quiet = _run("solomon.cli", "skills", env_extra={"OCTAVRYN_SUPPRESS_DEPRECATION": "1"})
    assert "deprecated" not in quiet.stderr


def test_addon_canonical_key_and_manifest_name(tmp_path):
    from solomon.addon_manager import AddonState, discover_addons

    d = tmp_path / "a1"
    d.mkdir()
    (d / "octavryn-addon.yaml").write_text(
        'id: a1\nname: A1\nversion: "1"\naddon_api: "1"\noctavryn_compatibility: ">=0.5,<0.6"\n'
        "entrypoint: src/main.py\n", encoding="utf-8")
    old = tmp_path / "a2"
    old.mkdir()
    (old / "solomon-addon.yaml").write_text(
        'id: a2\nname: A2\nversion: "1"\naddon_api: "1"\nsolomon_compatibility: ">=0.4,<0.5"\n'
        "entrypoint: src/main.py\n", encoding="utf-8")
    recs = {r.manifest.id: r for r in discover_addons(tmp_path)}
    assert recs["a1"].state != AddonState.QUARANTINED
    assert recs["a2"].state == AddonState.QUARANTINED  # declared <0.5, running 0.5 -> fail closed
