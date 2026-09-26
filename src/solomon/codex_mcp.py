"""Codex CLI / ChatGPT Desktop MCP integration (D69).

On this machine "ChatGPT Desktop" is the MSIX package `OpenAI.Codex`
(display name ChatGPT; it bundles codex.exe). Its agent reads the same
user config as the Codex CLI, `~/.codex/config.toml` (the app itself
writes its own `mcp_servers.node_repl` entry there). The supported way to
give it Octavryn is therefore a *local stdio MCP server entry* in that
file. ChatGPT's plain chat mode only accepts remote HTTPS connectors,
which Octavryn does not expose (that would open a network listener), so
it is deliberately not used.

Least privilege: the entry carries `enabled_tools` with exactly the four
read-only tools, so even a future server version with more tools would
not be offered to Codex/ChatGPT without editing this allowlist.

The file is TOML with keys this module does not own, and Python only has
a TOML *reader*. So the entry is written as a marked text block appended
at the end (a new table can always be appended), and removal deletes only
that marked block. Every write: backup next to the file, write to a temp
file, re-parse with tomllib and check that every pre-existing top-level
key and every other MCP server is unchanged, then atomically replace. Any
check failure leaves the original file untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .desktop_mcp import MODULE, SERVER_KEY, client_src_dir, verify as verify_stdio
from .surfaces import READ_ONLY_MCP_TOOLS

BEGIN = "# BEGIN octavryn (managed by `octavryn codex-mcp`; read-only tools only)"
END = "# END octavryn"


def default_config() -> Path:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return Path(home) / "config.toml"


def _toml_str(value: str) -> str:
    if "'" not in value and "\n" not in value:
        return f"'{value}'"  # literal string: backslashes kept as-is
    return json.dumps(value)  # basic string with escapes


def planned_block(python: str | None = None, src_dir: Path | None = None) -> str:
    tools = ", ".join(json.dumps(t) for t in READ_ONLY_MCP_TOOLS)
    return "\n".join([
        BEGIN,
        f"[mcp_servers.{SERVER_KEY}]",
        f"command = {_toml_str(python or sys.executable)}",
        f'args = ["-m", "{MODULE}"]',
        f"enabled_tools = [{tools}]",
        "startup_timeout_sec = 30",
        "",
        f"[mcp_servers.{SERVER_KEY}.env]",
        f"PYTHONPATH = {_toml_str(str(src_dir or client_src_dir()))}",
        'OCTAVRYN_SUPPRESS_DEPRECATION = "1"',
        END,
        "",
    ])


def _parse(text: str) -> dict:
    return tomllib.loads(text)


def status(path: Path) -> dict:
    if not path.exists():
        return {"config": str(path), "exists": False, "servers": [], "octavryn_registered": False}
    data = _parse(path.read_text(encoding="utf-8"))
    servers = data.get("mcp_servers") or {}
    ours = servers.get(SERVER_KEY) or {}
    return {
        "config": str(path), "exists": True, "servers": sorted(servers),
        "octavryn_registered": SERVER_KEY in servers,
        "octavryn_enabled_tools": ours.get("enabled_tools"),
        "managed_block": BEGIN in path.read_text(encoding="utf-8"),
    }


def _write_checked(path: Path, new_text: str, expect_ours: bool) -> Path:
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    old = _parse(old_text)
    new = _parse(new_text)  # raises on invalid TOML -> nothing written
    old_servers = dict(old.get("mcp_servers") or {})
    new_servers = dict(new.get("mcp_servers") or {})
    old_servers.pop(SERVER_KEY, None)
    if (SERVER_KEY in new_servers) != expect_ours:
        raise ValueError("octavryn entry state after edit is not what was intended")
    new_servers.pop(SERVER_KEY, None)
    if old_servers != new_servers:
        raise ValueError("edit would change other MCP servers; refusing")
    for key in set(old) | set(new):
        if key != "mcp_servers" and old.get(key) != new.get(key):
            raise ValueError(f"edit would change top-level key '{key}'; refusing")
    backup = path.with_name(path.name + f".bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    if path.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".octavryn.tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="")
    _replace_with_retry(tmp, path)
    return backup


def _replace_with_retry(src: Path, dst: Path, attempts: int = 10) -> None:
    """D75: on Windows os.replace can fail with a transient 'access denied'
    while another process (antivirus, the Codex app reading its config)
    has the file open. Retry briefly; re-raise if it persists (the
    original file is untouched in that case)."""
    import time

    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                src.unlink(missing_ok=True)
                raise
            time.sleep(0.2 * (i + 1))


def apply(path: Path, block: str | None = None) -> dict:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if SERVER_KEY in (_parse(text).get("mcp_servers") or {}):
        return {"action": "none", "reason": "already registered", "config": str(path)}
    sep = "" if text.endswith("\n") or not text else "\n"
    backup = _write_checked(path, text + sep + "\n" + (block or planned_block()), expect_ours=True)
    return {"action": "added", "config": str(path), "backup": str(backup)}


def remove(path: Path) -> dict:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if SERVER_KEY not in (_parse(text).get("mcp_servers") or {}):
        return {"action": "none", "reason": "not registered", "config": str(path)}
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        return {"action": "none", "reason": "octavryn entry exists but was not added by codex-mcp; edit it by hand",
                "config": str(path)}
    end += len(END)
    if text[end:end + 1] == "\n":
        end += 1
    new_text = text[:start].rstrip("\n") + "\n" + text[end:]
    backup = _write_checked(path, new_text, expect_ours=False)
    return {"action": "removed", "config": str(path), "backup": str(backup)}


def verify() -> dict:
    """Stdio handshake with exactly the planned command, then check that
    the allowlist covers every tool the server offers (nothing extra)."""
    out = verify_stdio()
    out["enabled_tools"] = list(READ_ONLY_MCP_TOOLS)
    out["unlisted_server_tools"] = sorted(set(out.get("tools") or []) - set(READ_ONLY_MCP_TOOLS))
    out["ok"] = bool(out["ok"]) and not out["unlisted_server_tools"]
    return out


def _cmd(args) -> int:
    path = Path(args.config) if args.config else default_config()
    action = args.codex_action
    if action == "plan":
        print(planned_block())
        return 0
    if action == "verify":
        out = verify()
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 1
    if action == "status":
        print(json.dumps(status(path), indent=2))
        return 0
    try:
        out = apply(path) if action == "apply" else remove(path)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Refused, config unchanged: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def add_codex_mcp_parsers(sub) -> None:
    p = sub.add_parser("codex-mcp", help="D69: offer the read-only Octavryn MCP tools to Codex CLI / ChatGPT Desktop")
    p.add_argument("codex_action", choices=["status", "plan", "verify", "apply", "remove"])
    p.add_argument("--config", default=None, help="config.toml path (default: $CODEX_HOME or ~/.codex/config.toml)")
    p.set_defaults(func=_cmd)
