"""v0.5 Claude Desktop MCP integration (D63). All config edits go to temp
files; the real Desktop config is never written by tests."""

import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon import desktop_mcp as dm


def cfg(tmp_path, data):
    p = tmp_path / "claude_desktop_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_apply_preserves_other_keys_backs_up_and_is_idempotent(tmp_path):
    p = cfg(tmp_path, {"preferences": {"x": 1}, "mcpServers": {"other": {"command": "node"}}})
    out = dm.apply(p)
    assert out["action"] == "added" and pathlib.Path(out["backup"]).exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["preferences"] == {"x": 1} and "other" in data["mcpServers"]
    assert data["mcpServers"]["octavryn"]["args"] == ["-m", "octavryn.mcp_server"]
    assert dm.apply(p)["action"] == "none"


def test_remove_only_touches_octavryn(tmp_path):
    p = cfg(tmp_path, {"mcpServers": {"octavryn": {"command": "x"}, "other": {"command": "node"}}})
    assert dm.remove(p)["action"] == "removed"
    assert json.loads(p.read_text(encoding="utf-8"))["mcpServers"] == {"other": {"command": "node"}}
    assert dm.remove(p)["action"] == "none"


def test_apply_to_missing_config_creates_it_without_backup(tmp_path):
    p = tmp_path / "new" / "claude_desktop_config.json"
    out = dm.apply(p)
    assert out["backup"] is None and dm.status(p)["octavryn_registered"]


def test_refuses_non_object_config(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        dm.apply(p)


def test_find_configs_msix_and_classic(tmp_path):
    msix = tmp_path / "local" / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude"
    msix.mkdir(parents=True)
    (msix / "claude_desktop_config.json").write_text("{}")
    classic = tmp_path / "roaming" / "Claude"
    classic.mkdir(parents=True)
    (classic / "claude_desktop_config.json").write_text("{}")
    found = dm.find_configs({"LOCALAPPDATA": str(tmp_path / "local"), "APPDATA": str(tmp_path / "roaming")})
    assert len(found) == 2


def test_planned_entry_has_no_credentials_and_is_read_only_server():
    e = dm.planned_entry()
    assert set(e["env"]) == {"PYTHONPATH", "OCTAVRYN_SUPPRESS_DEPRECATION"}
    assert e["args"] == ["-m", "octavryn.mcp_server"]


def test_verify_performs_real_handshake_with_read_only_tools():
    out = dm.verify()
    assert out["ok"] and out["server_name"] == "octavryn"
    assert out["tools"] == ["get_dashboard", "get_usage", "list_approvals", "list_projects"]


def test_cli_apply_requires_explicit_config(capsys):
    from solomon import cli
    assert cli.main(["desktop-mcp", "apply"]) in (1, 2)
