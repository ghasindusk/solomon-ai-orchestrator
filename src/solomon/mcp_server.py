"""Octavryn read-only MCP server (v0.4 D12 -> D26; renamed in v0.5, D52).

Exposes exactly the 4-tool surface identified as feasible back in
08_Discovery/PHASE0_DISCOVERY_REPORT.md's D12 investigation:
list_projects, get_dashboard, get_usage, list_approvals. All read-only:
no task creation, no approval decisions, no code execution, no addon
loading. This is a second transport (MCP, for other agents/tools) over
the same read-only surface `octavryn dashboard`/`project-list`/`usage`/
`approvals list` already expose over the CLI, not new capability.

v0.5 rename (D52). Clients see tool names as mcp__<config key>__<tool>,
so the config key is what really changes. That is migrated separately
(`octavryn mcp-migrate`, see mcp_migration.py). This module serves the
same four tools under either server name:
  python -m octavryn.mcp_server   canonical, server name "octavryn"
  python -m solomon.mcp_server    deprecated alias for the v0.5 line,
                                  server name "solomon", same tools
The alias exists so an existing `solomon` client registration keeps
working until the user removes it.

v0.5 privacy: list_approvals passes free-text fields through the same
credential redaction as export-log/diagnostics before they leave the
process.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .adapters.registry import known_adapter_names as _known_adapter_names
from .cli import _load_adapter
from .dashboard import build_dashboard, render_dashboard
from .knowledge import redact_secrets
from .registry import ProjectRegistry
from .state import StateStore
from .telemetry import get_gpu_telemetry
from .usage import UsageManager

CANONICAL_NAME = "octavryn"
LEGACY_NAME = "solomon"
READ_ONLY_TOOLS = ("list_projects", "get_dashboard", "get_usage", "list_approvals")

_DASHBOARD_ADAPTERS = _known_adapter_names()


def list_projects(include_superseded: bool = False) -> list[dict]:
    """List Octavryn's registered projects (project_id, name, status, tags)."""
    registry = ProjectRegistry()
    return [
        {
            "project_id": p.project_id,
            "name": p.name,
            "status": p.status,
            "tags": p.tags,
        }
        for p in registry.list_projects(include_superseded=include_superseded)
    ]


def get_dashboard(project_id: str | None = None) -> str:
    """Plain-text Octavryn dashboard snapshot (same as `octavryn dashboard`):
    per-project task/usage/knowledge summary, adapter health, active task
    queue, pending approvals, and a live GPU telemetry snapshot."""
    store = StateStore()
    registry = ProjectRegistry()
    health_checks = {name: _load_adapter(name).health() for name in _DASHBOARD_ADAPTERS}
    gpu = get_gpu_telemetry()
    data = build_dashboard(store, registry, health_checks, project_id=project_id, gpu_telemetry=gpu)
    return render_dashboard(data)


def get_usage(project_id: str | None = None) -> dict:
    """Real cost/token usage for a project (or all projects), with provenance."""
    store = StateStore()
    manager = UsageManager(store)
    return manager.get_project_usage(project_id)


def _redact(value):
    if isinstance(value, str):
        return redact_secrets(value)[0]
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def list_approvals(status: str = "pending") -> list[dict]:
    """List Octavryn approval requests by status (default: pending).
    Credential-like substrings in reasons/prompts are redacted."""
    store = StateStore()
    return [_redact(r) for r in store.list_approval_requests(status=status)]


def build_server(name: str = CANONICAL_NAME) -> MCPServer:
    if name not in (CANONICAL_NAME, LEGACY_NAME):
        raise ValueError(f"unknown server name {name!r}")
    server = MCPServer(name)
    for fn in (list_projects, get_dashboard, get_usage, list_approvals):
        server.tool()(fn)
    return server


# Module-level instance kept for callers/tests that import `mcp` (v0.4 API).
mcp = build_server(CANONICAL_NAME)


def main(name: str = CANONICAL_NAME) -> None:
    build_server(name).run()


if __name__ == "__main__":
    # Invoked as `python -m solomon.mcp_server`: the deprecated alias.
    # Notice goes to stderr only; stdout is the stdio MCP transport.
    import sys

    print("note: MCP server 'solomon' is deprecated; register 'octavryn' "
          "(python -m octavryn.mcp_server). Same read-only tools.", file=sys.stderr)
    main(LEGACY_NAME)
