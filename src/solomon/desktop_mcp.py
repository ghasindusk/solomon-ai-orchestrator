"""Claude Desktop MCP integration (v0.5 spec 06, D63).

Offers Octavryn's *read-only* MCP tools to Claude Desktop, a human-facing
surface. Nothing here grants execution authority: the server exposes only
list_projects / get_dashboard / get_usage / list_approvals (no approve,
execute or write tools).

- status: find the Desktop config (MSIX or classic install) and report
  whether an `octavryn` server entry exists (server names only; env values
  are never printed).
- plan: the exact JSON entry that would be added. Pure; writes nothing.
- apply / remove: operate on an explicit config path. The previous file is
  backed up next to it, the new file is written atomically (temp +
  replace), and every other key in the config is preserved as-is. Claude
  Desktop rewrites this file while it runs, so close it first (the
  command says so).
- verify: launch exactly the planned command over stdio and perform a real
  MCP initialize + list_tools handshake, i.e. what Claude Desktop would do,
  without touching the Desktop config.

No credential is involved: the entry holds a Python path and PYTHONPATH.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SERVER_KEY = "octavryn"
MODULE = "octavryn.mcp_server"
SRC_DIR = Path(__file__).resolve().parents[1]


def client_src_dir() -> Path:
    """The PYTHONPATH written into client configs. Prefers
    %OCTAVRYN_HOME%/src (set by the octavryn launcher; on this machine the
    docs junction, which is what the existing Claude Code/Codex entries
    use) over the resolved real path, so every registration points at the
    same place."""
    home = os.environ.get("OCTAVRYN_HOME")
    if home and (Path(home) / "src" / "octavryn").is_dir():
        return Path(home) / "src"
    return SRC_DIR


def find_configs(env: dict | None = None) -> list[Path]:
    env = env if env is not None else os.environ
    out = []
    local, roaming = env.get("LOCALAPPDATA"), env.get("APPDATA")
    if local:
        out += [Path(p) for p in glob.glob(str(Path(local) / "Packages" / "Claude_*" / "LocalCache" / "Roaming"
                                              / "Claude" / "claude_desktop_config.json"))]
    if roaming:
        p = Path(roaming) / "Claude" / "claude_desktop_config.json"
        if p.exists():
            out.append(p)
    return out


def planned_entry(python: str | None = None, src_dir: Path | None = None) -> dict:
    return {
        "command": python or sys.executable,
        "args": ["-m", MODULE],
        "env": {"PYTHONPATH": str(src_dir or client_src_dir()), "OCTAVRYN_SUPPRESS_DEPRECATION": "1"},
    }


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; refusing to edit it")
    return data


def status(path: Path) -> dict:
    data = _read(path)
    servers = data.get("mcpServers") or {}
    return {"config": str(path), "exists": path.exists(), "servers": sorted(servers),
            "octavryn_registered": SERVER_KEY in servers}


def _write_atomic(path: Path, data: dict) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(path.name + f".bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return backup


def desktop_running() -> bool | None:
    """D74: True when a Claude Desktop (MSIX or classic install) process is
    running. Claude Desktop rewrites claude_desktop_config.json from its
    in-memory state, and on 2026-09-26 it dropped an entry written while it
    ran. None = could not tell (non-Windows or the query failed)."""
    if not sys.platform.startswith("win"):
        return None
    import subprocess

    ps = (r"Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*\WindowsApps\Claude_*' "
          r"-or $_.ExecutablePath -like '*\AnthropicClaude\*' } | Measure-Object | Select-Object -ExpandProperty Count")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],  # nosec B603 B607 - fixed read-only query
                             capture_output=True, encoding="utf-8", errors="replace", timeout=60)
        return int(out.stdout.strip() or "0") > 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def apply(path: Path, entry: dict | None = None) -> dict:
    data = _read(path)
    servers = dict(data.get("mcpServers") or {})
    if SERVER_KEY in servers:
        return {"action": "none", "reason": "already registered", "config": str(path)}
    servers[SERVER_KEY] = entry or planned_entry()
    data["mcpServers"] = servers
    backup = _write_atomic(path, data)
    return {"action": "added", "config": str(path), "backup": str(backup) if backup else None}


def remove(path: Path) -> dict:
    data = _read(path)
    servers = dict(data.get("mcpServers") or {})
    if SERVER_KEY not in servers:
        return {"action": "none", "reason": "not registered", "config": str(path)}
    del servers[SERVER_KEY]
    data["mcpServers"] = servers
    backup = _write_atomic(path, data)
    return {"action": "removed", "config": str(path), "backup": str(backup) if backup else None}


def verify(entry: dict | None = None, timeout_s: float = 60) -> dict:
    """Real stdio handshake with the planned command (no config touched)."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    entry = entry or planned_entry()
    env = dict(os.environ)
    env.update(entry.get("env") or {})

    async def run():
        params = StdioServerParameters(command=entry["command"], args=entry["args"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                return init.server_info.name, sorted(t.name for t in tools.tools)

    name, tools = asyncio.run(asyncio.wait_for(run(), timeout_s))
    return {"ok": name == SERVER_KEY, "server_name": name, "tools": tools}


def _shape(entry: dict) -> dict:
    return {"command": entry["command"], "args": entry["args"], "env_keys": sorted(entry.get("env") or {})}


def _cmd(args) -> int:
    paths = [Path(args.config)] if args.config else find_configs()
    if args.desktop_action == "verify":
        out = verify()
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 1
    if args.desktop_action == "plan":
        print(json.dumps({"configs": [str(p) for p in paths], "entry": _shape(planned_entry())}, indent=2))
        return 0
    if not paths:
        print("Claude Desktop config not found; pass --config", file=sys.stderr)
        return 1
    if args.desktop_action == "status":
        out = [status(p) for p in paths]
        running = desktop_running()
        for o in out:
            o["desktop_running"] = running
        print(json.dumps(out, indent=2))
        return 0
    if not args.config:
        print("apply/remove need an explicit --config path (close Claude Desktop first)", file=sys.stderr)
        return 2
    if not getattr(args, "allow_running", False) and desktop_running():
        print("Claude Desktop is running and would overwrite the config from memory (D74). Quit it fully "
              "(tray icon -> Quit), then run this again.", file=sys.stderr)
        return 3
    fn = apply if args.desktop_action == "apply" else remove
    print(json.dumps(fn(Path(args.config)), indent=2))
    return 0


def add_desktop_mcp_parsers(sub) -> None:
    p = sub.add_parser("desktop-mcp", help="v0.5: offer the read-only Octavryn MCP tools to Claude Desktop")
    p.add_argument("desktop_action", choices=["status", "plan", "verify", "apply", "remove"])
    p.add_argument("--config", default=None, help="Claude Desktop config path (required for apply/remove)")
    p.add_argument("--allow-running", action="store_true",
                   help="D74: skip the 'Claude Desktop must be quit' check (tests / non-standard installs)")
    p.set_defaults(func=_cmd)
