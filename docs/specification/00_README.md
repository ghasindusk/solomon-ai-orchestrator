# Solomon AI Orchestrator v0.4

Progress: 100% design baseline / 0% implementation

## Definition
Solomon is an invisible local multi-AI orchestration runtime. Users continue working normally in their preferred AI interface (for example Codex or Claude Code). A gateway delegates suitable requests to Solomon in the background. Solomon reconstructs project context, decomposes the goal, assigns roles, routes tasks to appropriate agents, verifies results, records evidence, updates project knowledge, and returns the integrated result to the original caller.

## Core principles
- Invisible orchestration: no requirement to explicitly say "use Solomon".
- Non-conversational by default: agents exchange structured task/result objects through Solomon.
- Role != Agent: roles are selected first; providers are dynamically assigned.
- Local-first knowledge: LocalAI/Ollama + Obsidian can serve as the knowledge/context layer.
- Safety above autonomy: policy and approval gates cannot be overridden by agents/addons.
- Durable execution: state survives interruption and can resume.
- Observable and replayable: event sourcing, project logs, agent logs, routing evidence.
- Extensible: Addon SDK, MCP tool integration, A2A external-agent transport.
- Privacy-first: telemetry off by default.
- Optional Jev decision layer: useful for typed routing decisions, never a mandatory dependency.

## Initial agents
- LocalAI (Ollama)
- Claude Code
- Codex
- Antigravity
- Jev (optional decision adapter)

## Package
See each numbered directory for the formal specification, architecture, gateway/runtime contract, addon SDK, logging/project layout, policies, schemas, GitHub publication plan, branding brief, roadmap and templates.
