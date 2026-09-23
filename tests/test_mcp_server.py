import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import solomon.mcp_server as srv


def test_all_four_read_only_tools_are_registered():
    async def _list():
        return await srv.mcp.list_tools()

    tools = asyncio.run(_list())
    names = {t.name for t in tools}
    assert names == {"list_projects", "get_dashboard", "get_usage", "list_approvals"}


# Note: the development repository's test suite includes an additional
# test here (test_list_projects_returns_real_registry_entries) that
# asserts a specific project_id is present in that developer's real
# registry. Not included in this public release for the same reason as
# tests/test_registry.py -- it would only ever skip against the bundled
# example registry.


def test_get_usage_returns_provenance():
    usage = srv.get_usage("solomon_ai_orchestrator")
    assert "cost_provenance" in usage


def test_list_approvals_returns_a_list():
    approvals = srv.list_approvals()
    assert isinstance(approvals, list)
