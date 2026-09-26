"""v0.5 MCP rename (D52): real stdio connection tests for the canonical
and legacy server names, redaction, and the client-registration
migration planner/applier (with an injected CLI runner, never the real
client config)."""

import asyncio
import os
import sys
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import pytest

from solomon import mcp_migration as mm
from solomon import mcp_server as srv
from solomon import state as state_mod
from solomon.state import StateStore


# --- real MCP connection over stdio ------------------------------------------------

async def _connect(module: str, tmp_db: pathlib.Path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["OCTAVRYN_DB"] = str(tmp_db)  # never touch the real state from a test
    params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("list_approvals", {"status": "pending"})
            return init.server_info.name, sorted(t.name for t in tools.tools), result


@pytest.mark.parametrize("module,expected_name", [
    ("octavryn.mcp_server", "octavryn"),
    ("solomon.mcp_server", "solomon"),  # deprecated alias still serves
])
def test_stdio_connection_initialize_list_and_call(tmp_path, module, expected_name):
    s = StateStore(tmp_path / "mcp.sqlite3")
    s.create_approval_request("appr-x", "p", "HIGH", "uses sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
                              {"prompt": "token: abcdefghijklmnop1234"})
    s.close()
    name, tools, result = asyncio.run(asyncio.wait_for(_connect(module, tmp_path / "mcp.sqlite3"), 60))
    assert name == expected_name
    assert tools == sorted(srv.READ_ONLY_TOOLS)
    assert not result.is_error
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert "appr-x" in text
    assert "sk-ant-api03" not in text and "abcdefghijklmnop1234" not in text  # redacted on the wire


def test_both_names_expose_identical_read_only_tools():
    async def names(server):
        return sorted(t.name for t in await server.list_tools())
    assert asyncio.run(names(srv.build_server("octavryn"))) == asyncio.run(names(srv.build_server("solomon")))
    with pytest.raises(ValueError):
        srv.build_server("anything_else")


def test_no_write_tools_exposed():
    async def names():
        return {t.name for t in await srv.mcp.list_tools()}
    for forbidden in ("decide", "approve", "execute", "run", "delete"):
        assert not any(forbidden in n for n in asyncio.run(names()))


def test_list_approvals_redacts_in_process(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_DEFAULT_DB_PATH", tmp_path / "s.sqlite3")
    StateStore().create_approval_request("a", "p", "HIGH", "key sk-abcdefghijklmnopqrstuvwxyz0", {"n": [1, "x"]})
    [row] = srv.list_approvals()
    assert "sk-abcdefghij" not in row["reason"] and "[REDACTED]" in row["reason"]
    assert row["task_params"] == {"n": [1, "x"]}  # non-strings untouched


# --- registration migration --------------------------------------------------------------

PY = r"C:\Python\python.exe"


def config(**extra):
    base = {
        "mcpServers": {"ollama": {"type": "stdio", "command": "npx", "args": ["-y", "ollama-mcp-server"]}},
        "projects": {
            "D:/work/me": {"mcpServers": {
                "solomon": {"type": "stdio", "command": PY, "args": ["-m", "solomon.mcp_server"],
                            "env": {"PYTHONPATH": r"C:\x\src", "SECRETISH": "do-not-print"}},
                "memory": {"command": "npx", "args": ["x"]},
            }},
        },
    }
    base.update(extra)
    return base


class FakeCLI:
    def __init__(self, fail_add=False):
        self.calls, self.fail_add = [], fail_add

    def __call__(self, argv, cwd):
        self.calls.append((argv[1:], cwd))
        if self.fail_add and "add-json" in argv:
            return 1, "boom"
        return 0, "ok"


def test_plan_finds_legacy_and_builds_canonical_entry():
    [a] = mm.plan(config())
    assert a["status"] == "add" and a["scope"] == "local" and a["project"] == "D:/work/me"
    assert a["new_entry"]["args"] == ["-m", "octavryn.mcp_server"]
    assert a["new_entry"]["env"] == config()["projects"]["D:/work/me"]["mcpServers"]["solomon"]["env"]


def test_describe_never_leaks_env_values():
    d = mm.describe(mm.plan(config())[0])
    assert d["env_keys"] == ["PYTHONPATH", "SECRETISH"]
    assert "do-not-print" not in str(d)


def test_apply_uses_official_cli_with_scope_and_keeps_legacy(monkeypatch):
    monkeypatch.setattr(mm, "_claude", lambda: "claude")
    fake = FakeCLI()
    res = mm.apply(mm.plan(config()), runner=fake)
    assert [r["action"] for r in res] == ["add"]
    (argv, cwd), = fake.calls
    assert argv[:5] == ["mcp", "add-json", "-s", "local", "octavryn"] and cwd == "D:/work/me"


def test_apply_is_idempotent_once_octavryn_exists(monkeypatch):
    monkeypatch.setattr(mm, "_claude", lambda: "claude")
    cfg = config()
    cfg["projects"]["D:/work/me"]["mcpServers"]["octavryn"] = {"command": PY, "args": ["-m", "octavryn.mcp_server"]}
    [a] = mm.plan(cfg)
    assert a["status"] == "done"
    fake = FakeCLI()
    assert mm.apply([a], runner=fake) == [] and fake.calls == []


def test_failed_add_never_removes_legacy(monkeypatch):
    monkeypatch.setattr(mm, "_claude", lambda: "claude")
    fake = FakeCLI(fail_add=True)
    res = mm.apply(mm.plan(config()), runner=fake, remove_legacy=True)
    assert [r["action"] for r in res] == ["add"] and res[0]["rc"] == 1
    assert not any("remove" in c[0] for c in fake.calls)


def test_remove_legacy_is_explicit(monkeypatch):
    monkeypatch.setattr(mm, "_claude", lambda: "claude")
    fake = FakeCLI()
    mm.apply(mm.plan(config()), runner=fake, remove_legacy=True)
    assert [c[0][:2] for c in fake.calls] == [["mcp", "add-json"], ["mcp", "remove"]]
    assert fake.calls[1][0][-1] == "solomon"


def test_rollback_removes_only_octavryn_and_only_with_legacy_present(monkeypatch):
    monkeypatch.setattr(mm, "_claude", lambda: "claude")
    cfg = config()
    cfg["projects"]["D:/work/me"]["mcpServers"]["octavryn"] = {"command": PY, "args": ["-m", "octavryn.mcp_server"]}
    fake = FakeCLI()
    [r] = mm.rollback(cfg, runner=fake)
    assert r["action"] == "remove_octavryn" and fake.calls[0][0][-1] == "octavryn"
    # without a legacy entry, rollback refuses (would leave no server)
    del cfg["projects"]["D:/work/me"]["mcpServers"]["solomon"]
    fake2 = FakeCLI()
    [r2] = mm.rollback(cfg, runner=fake2)
    assert r2["action"] == "skip" and fake2.calls == []


def test_unrelated_servers_and_foreign_solomon_key_are_ignored():
    cfg = {"mcpServers": {"solomon": {"command": "node", "args": ["some-other-solomon.js"]}}}
    assert mm.plan(cfg) == []
