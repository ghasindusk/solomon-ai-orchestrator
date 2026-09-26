import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

import solomon.mcp_server as srv


def test_all_four_read_only_tools_are_registered():
    async def _list():
        return await srv.mcp.list_tools()

    tools = asyncio.run(_list())
    names = {t.name for t in tools}
    assert names == {"list_projects", "get_dashboard", "get_usage", "list_approvals"}


def test_list_projects_returns_real_registry_entries():
    """v0.4 D38: only meaningful against this developer's real registry
    (gitignored) -- a fresh checkout gets the bootstrapped
    .example.yaml instead (conftest.py), which has no
    "solomon_ai_orchestrator" entry. Skip rather than fail there."""
    projects = srv.list_projects()
    assert isinstance(projects, list)
    assert all({"project_id", "name", "status", "tags"} <= set(p.keys()) for p in projects)
    if not any(p["project_id"] == "solomon_ai_orchestrator" for p in projects):
        pytest.skip("real projects.registry.yaml not present in this environment")


def test_get_usage_returns_provenance():
    usage = srv.get_usage("solomon_ai_orchestrator")
    assert "cost_provenance" in usage


def test_list_approvals_returns_a_list():
    approvals = srv.list_approvals()
    assert isinstance(approvals, list)
