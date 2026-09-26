"""Canonical Octavryn MCP server entry point (v0.5, D52):
`python -m octavryn.mcp_server`. Same read-only tools as the deprecated
`python -m solomon.mcp_server`, served under the name "octavryn"."""

from __future__ import annotations

from solomon.mcp_server import CANONICAL_NAME, main

if __name__ == "__main__":
    main(CANONICAL_NAME)
