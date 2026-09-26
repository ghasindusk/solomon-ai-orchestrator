"""MCP client registration migration: `solomon` -> `octavryn` (v0.5, D52).

Claude Code names MCP tools mcp__<registration key>__<tool>, so renaming
the server means registering it under a new key. This module:

- reads ~/.claude.json read-only and finds registrations that run the
  Solomon MCP server (by module `solomon.mcp_server`, or key `solomon`
  pointing at our module), in user scope and per-project ("local") scope;
- plans an `octavryn` registration for each: same command and env, with
  the module switched to `octavryn.mcp_server`;
- applies it only through the official client CLI (`claude mcp add-json
  -s <scope>`), never by rewriting ~/.claude.json directly. Claude Code
  rewrites that file while running, so a direct edit could race it;
- keeps the legacy `solomon` registration by default (deprecated alias).
  Removing it is a separate explicit step (`--remove-legacy`), and so is
  rolling back (`rollback` removes only the `octavryn` key this tool
  added).

Only the registration's shape is printed. env *values* are never
printed, since an env block could hold secrets. Our entry only has
PYTHONPATH, but the tool does not assume that.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv to the local `claude` CLI, no shell
import sys
from dataclasses import dataclass
from pathlib import Path

LEGACY_KEY = "solomon"
NEW_KEY = "octavryn"
LEGACY_MODULE = "solomon.mcp_server"
NEW_MODULE = "octavryn.mcp_server"


@dataclass
class Registration:
    scope: str            # "user" | "local"
    project: str | None   # project path for local scope
    key: str
    entry: dict

    @property
    def runs_our_server(self) -> bool:
        args = [str(a) for a in self.entry.get("args") or []]
        return LEGACY_MODULE in args or NEW_MODULE in args


def default_config_path() -> Path:
    return Path(os.path.expanduser("~")) / ".claude.json"


def load_registrations(config: dict) -> list[Registration]:
    regs = [Registration("user", None, k, v) for k, v in (config.get("mcpServers") or {}).items()]
    for proj, pc in (config.get("projects") or {}).items():
        for k, v in (pc.get("mcpServers") or {}).items():
            regs.append(Registration("local", proj, k, v))
    return regs


def plan(config: dict) -> list[dict]:
    """One action per scope/project that has a legacy registration and no
    `octavryn` one yet. Idempotent: an existing `octavryn` key means done."""
    regs = load_registrations(config)
    actions = []
    for r in regs:
        if not r.runs_our_server or LEGACY_MODULE not in [str(a) for a in r.entry.get("args") or []]:
            continue
        already = any(x.key == NEW_KEY and x.scope == r.scope and x.project == r.project for x in regs)
        new_entry = dict(r.entry)
        new_entry["args"] = [NEW_MODULE if str(a) == LEGACY_MODULE else a for a in r.entry.get("args") or []]
        actions.append({
            "scope": r.scope, "project": r.project, "legacy_key": r.key,
            "status": "done" if already else "add",
            "new_entry": new_entry,
        })
    return actions


def describe(action: dict) -> dict:
    """Shape only: env values are masked."""
    e = action["new_entry"]
    return {
        "scope": action["scope"], "project": action["project"], "legacy_key": action["legacy_key"],
        "status": action["status"], "command": e.get("command"), "args": e.get("args"),
        "env_keys": sorted((e.get("env") or {}).keys()),
    }


def _claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` CLI not found on PATH")
    return exe


def apply(actions: list[dict], runner=None, remove_legacy: bool = False) -> list[dict]:
    """runner(argv, cwd) -> (returncode, output). Injected in tests; the
    default calls the real `claude` CLI with a fixed argv and no shell."""
    runner = runner or _run
    results = []
    for a in actions:
        cwd = a["project"] if a["scope"] == "local" else None
        if a["status"] == "add":
            payload = json.dumps(a["new_entry"], ensure_ascii=False)
            rc, out = runner([_claude(), "mcp", "add-json", "-s", a["scope"], NEW_KEY, payload], cwd)
            results.append({"action": "add", "scope": a["scope"], "project": a["project"], "rc": rc,
                            "output": out[-300:]})
            if rc != 0:
                continue  # never remove the legacy key if adding the new one failed
        if remove_legacy:
            rc, out = runner([_claude(), "mcp", "remove", "-s", a["scope"], a["legacy_key"]], cwd)
            results.append({"action": "remove_legacy", "scope": a["scope"], "project": a["project"], "rc": rc,
                            "output": out[-300:]})
    return results


def rollback(config: dict, runner=None) -> list[dict]:
    """Removes `octavryn` registrations that point at our server, and only
    where a legacy registration still exists (so rollback can never leave
    the user with no server at all)."""
    runner = runner or _run
    regs = load_registrations(config)
    out = []
    for r in regs:
        if r.key != NEW_KEY or not r.runs_our_server:
            continue
        has_legacy = any(x.key != NEW_KEY and x.runs_our_server and x.scope == r.scope and x.project == r.project
                         for x in regs)
        if not has_legacy:
            out.append({"action": "skip", "scope": r.scope, "project": r.project,
                        "reason": "no legacy registration to fall back to"})
            continue
        rc, text = runner([_claude(), "mcp", "remove", "-s", r.scope, NEW_KEY],
                          r.project if r.scope == "local" else None)
        out.append({"action": "remove_octavryn", "scope": r.scope, "project": r.project, "rc": rc,
                    "output": text[-300:]})
    return out


def _run(argv: list[str], cwd: str | None) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",  # nosec B603
                          timeout=60)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _read(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# -- CLI --------------------------------------------------------------------

def _cmd(args) -> int:
    path = Path(args.config) if args.config else default_config_path()
    config = _read(path)
    if args.mcp_action == "rollback":
        print(json.dumps(rollback(config), indent=2, ensure_ascii=False))
        return 0
    actions = plan(config)
    report = {"config": str(path), "actions": [describe(a) for a in actions]}
    if args.mcp_action == "apply":
        report["results"] = apply(actions, remove_legacy=args.remove_legacy)
        ok = all(r["rc"] == 0 for r in report["results"])
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if ok else 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not actions:
        print("no registration runs solomon.mcp_server; nothing to migrate", file=sys.stderr)
    return 0


def add_mcp_migration_parsers(sub) -> None:
    p = sub.add_parser("mcp-migrate", help="v0.5: migrate MCP client registration solomon -> octavryn")
    p.add_argument("mcp_action", choices=["status", "apply", "rollback"])
    p.add_argument("--config", default=None, help="Client config to read (default ~/.claude.json, read-only)")
    p.add_argument("--remove-legacy", dest="remove_legacy", action="store_true",
                   help="Also remove the deprecated `solomon` registration after adding `octavryn`")
    p.set_defaults(func=_cmd)
