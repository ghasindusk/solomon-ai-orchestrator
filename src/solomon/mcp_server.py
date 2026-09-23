"""Solomon read-only MCP server (v0.4 migration, D12 revisited -> D26).

Exposes exactly the 4-tool surface identified as feasible back in
08_Discovery/PHASE0_DISCOVERY_REPORT.md's D12 investigation:
list_projects, get_dashboard, get_usage, list_approvals. All read-only
-- no task creation, no approval decisions, no code execution, no addon
loading. This is a second transport (MCP, for other agents/tools) over
the same read-only surface `solomon dashboard`/`project-list`/`usage`/
`approvals list` already expose over the CLI, not new capability.

Run standalone for manual testing: `python -m solomon.mcp_server`
(stdio transport). Registration with a client (`claude mcp add` / `agy
mcp add`) is a separate step, e.g.
`claude mcp add solomon -- python -m solomon.mcp_server`.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .cli import _load_adapter
from .dashboard import build_dashboard, render_dashboard
from .registry import ProjectRegistry
from .state import StateStore
from .telemetry import get_gpu_telemetry
from .usage import UsageManager

mcp = MCPServer("solomon")

_DASHBOARD_ADAPTERS = ["claude_code", "codex", "localai_ollama", "antigravity"]


@mcp.tool()
def list_projects(include_superseded: bool = False) -> list[dict]:
    """List Solomon's registered projects (project_id, name, status, tags)."""
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


@mcp.tool()
def get_dashboard(project_id: str | None = None) -> str:
    """Plain-text Solomon dashboard snapshot (same as `solomon dashboard`):
    per-project task/usage/knowledge summary, adapter health, active task
    queue, pending approvals, and a live GPU telemetry snapshot."""
    store = StateStore()
    registry = ProjectRegistry()
    health_checks = {name: _load_adapter(name).health() for name in _DASHBOARD_ADAPTERS}
    gpu = get_gpu_telemetry()
    data = build_dashboard(store, registry, health_checks, project_id=project_id, gpu_telemetry=gpu)
    return render_dashboard(data)


@mcp.tool()
def get_usage(project_id: str | None = None) -> dict:
    """Real cost/token usage for a project (or all projects), with provenance."""
    store = StateStore()
    manager = UsageManager(store)
    return manager.get_project_usage(project_id)


@mcp.tool()
def list_approvals(status: str = "pending") -> list[dict]:
    """List Solomon approval requests by status (default: pending)."""
    store = StateStore()
    return store.list_approval_requests(status=status)


if __name__ == "__main__":
    mcp.run()
