"""Desktop Intelligence Surfaces (Octavryn SI v0.5 spec 06, roadmap R5).

ChatGPT Desktop and Claude Desktop are optional interaction surfaces,
never execution agents:
- detection is read-only filesystem inspection. It launches nothing and
  does no GUI automation, and Core works identically when every surface
  is absent (spec 06 "Core MUST NOT depend on GUI automation");
- SurfaceDescriptor.privileged_execution is always False. A surface can
  reach execution only by calling a supported bridge (MCP, connector,
  the v0.5 remote Task Bridge), and that path goes through governance
  like the CLI ("Conversational access MUST NOT imply privileged
  execution authority");
- Claude Desktop and Claude Code are separate participants: Claude Code
  is the `claude_code` adapter (an execution intelligence), while Claude
  Desktop is a surface here.

What each surface is offered: the read-only MCP tools from mcp_server.py,
and only when that surface's own config actually registers our MCP
server. Server names are read; commands, args and env are not (env can
hold API keys).
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

from .descriptors import Availability, SurfaceDescriptor

READ_ONLY_MCP_TOOLS = ["list_projects", "get_dashboard", "get_usage", "list_approvals"]
OUR_MCP_SERVER_NAMES = {"solomon", "octavryn"}


def _env_path(name: str, override: dict | None) -> Path | None:
    value = (override or {}).get(name, os.environ.get(name))
    return Path(value) if value else None


def _mcp_server_names(config_file: Path) -> list[str] | None:
    """None = no readable config (bridge unknown); [] = config without MCP."""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return sorted((data.get("mcpServers") or {}).keys())


def detect_claude_desktop(env: dict | None = None) -> SurfaceDescriptor:
    local = _env_path("LOCALAPPDATA", env)
    roaming = _env_path("APPDATA", env)
    installs: list[Path] = []
    configs: list[Path] = []
    if local:
        installs += [Path(p) for p in glob.glob(str(local / "Packages" / "Claude_*"))]
        installs += [p for p in [local / "AnthropicClaude"] if p.exists()]
        configs += [Path(p) for p in glob.glob(str(local / "Packages" / "Claude_*" / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"))]
    if roaming:
        configs += [p for p in [roaming / "Claude" / "claude_desktop_config.json"] if p.exists()]
    return _build(
        "claude_desktop", installs, configs,
        exposed=["conversation", "reasoning", "mcp_use"],
    )


def _codex_mcp_server_names(config_file: Path) -> list[str] | None:
    """Server names from ~/.codex/config.toml (TOML). None = unreadable."""
    import tomllib

    try:
        with open(config_file, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return None
    return sorted((data.get("mcp_servers") or {}).keys())


def detect_chatgpt_desktop(env: dict | None = None) -> SurfaceDescriptor:
    """D69: ChatGPT Desktop on Windows ships as the MSIX package
    `OpenAI.Codex` (display name "ChatGPT", bundles codex.exe); older
    builds used `OpenAI.ChatGPT*`. Its agent reads the Codex user config
    (~/.codex/config.toml, or $CODEX_HOME), which is where a local stdio
    MCP server can be offered. Plain chat mode only takes remote HTTPS
    connectors, which Octavryn does not expose."""
    local = _env_path("LOCALAPPDATA", env)
    installs: list[Path] = []
    if local:
        installs += [Path(p) for p in glob.glob(str(local / "Packages" / "OpenAI.ChatGPT*"))]
        installs += [Path(p) for p in glob.glob(str(local / "Packages" / "OpenAI.Codex_*"))]
        installs += [p for p in [local / "Programs" / "ChatGPT"] if p.exists()]
    codex_home = _env_path("CODEX_HOME", env)
    if codex_home is None:
        home = _env_path("USERPROFILE", env) or _env_path("HOME", env)
        codex_home = home / ".codex" if home else None
    names = None
    if codex_home is not None and (codex_home / "config.toml").exists():
        names = _codex_mcp_server_names(codex_home / "config.toml")
    desc = _build("chatgpt_desktop", installs, [], exposed=["conversation", "reasoning", "mcp_use"],
                  names_override=names)
    if desc.availability == Availability.AVAILABLE:
        desc.detail += " (bridge: local stdio MCP via the Codex config; chat-mode connectors not used)"
    return desc


def _build(surface_id: str, installs: list[Path], configs: list[Path], exposed: list[str],
           names_override: list[str] | None = None) -> SurfaceDescriptor:
    if not installs:
        return SurfaceDescriptor(
            id=surface_id, type="desktop_app", availability=Availability.ABSENT,
            bridge=None, detail="not installed (read-only filesystem check)",
        )
    names: list[str] | None = names_override
    for cfg in configs:
        found = _mcp_server_names(cfg)
        if found is not None:
            names = sorted(set(names or []) | set(found))
    offered: list[str] = []
    if names is None:
        bridge = None
        detail = "installed; no readable MCP config, bridge unknown"
    else:
        bridge = "mcp"
        ours = OUR_MCP_SERVER_NAMES & set(names)
        offered = list(READ_ONLY_MCP_TOOLS) if ours else []
        detail = f"installed; MCP servers configured: {len(names)}" + (
            f"; Octavryn read-only tools registered as {sorted(ours)}" if ours else "; Octavryn MCP not registered"
        )
    return SurfaceDescriptor(
        id=surface_id,
        type="desktop_app",
        availability=Availability.AVAILABLE,
        interaction_modes=["conversation"],
        bridge=bridge,
        permissions=[],
        human_presence_required=True,
        exposed_capabilities=exposed,
        octavryn_capabilities_offered=offered,
        detail=detail,
    )


def detect_surfaces(env: dict | None = None) -> list[SurfaceDescriptor]:
    return [detect_claude_desktop(env), detect_chatgpt_desktop(env)]
