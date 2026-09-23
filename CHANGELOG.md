# Changelog

All notable changes to Solomon AI Orchestrator are documented here. Format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This is a pre-1.0 alpha. Behavior, CLI flags, and schemas may change
without a deprecation period until v1.0.0.

## [0.4.0-alpha] - 2026-09-24

Initial public alpha. Implements the v0.4 specification's full
Phase 0-9 roadmap (`docs/specification/10_Roadmap/`).

### Added
- Core orchestration runtime: Goal/Task model, policy engine, SQLite
  event/state store, Definition of Done verification.
- Adapters for Claude Code, Codex, Antigravity, and local Ollama.
- Dynamic, history-informed routing (`router.py`) with GPU-aware
  penalties for local adapters.
- Invisible Gateway: an advisory-only `UserPromptSubmit` hook
  (`hooks/`) that can suggest delegating a request to Solomon, never
  blocks a prompt.
- Context Firewall: relevance-ranked, deduplicated, secret-redacted
  knowledge retrieval, with an explicit global knowledge scope.
- Token & Compute Intelligence: a structured `UsageRecord` model with
  explicit provenance (API_REPORTED/CLI_REPORTED/CALCULATED/ESTIMATED/
  UNKNOWN -- never silently coerced to 0), multi-axis aggregation, a
  Token Budget with Human Approval on hard-stop, and Token Efficiency
  as an auxiliary (not mechanical) routing signal.
- Addon manifest discovery and validation (execution/`ENABLED` state
  is not implemented -- see Known Limitations).
- A read-only MCP server exposing project/dashboard/usage/approval
  data.
- Replay and router-comparison tooling for evaluating a routing
  config change against historical tasks without mutating state.
- A categorized eval runner (`solomon eval`) grouping the test suite
  into routing/context_firewall/safety/recovery/addons/completion.
- Diagnostics export (`solomon diagnostics-export`), redacted by
  default; `--include-sensitive` opts into more detail with secret
  redaction still applied.
- A plain-text dashboard/status CLI (`solomon dashboard`, `usage`,
  `approvals`, `learning-report`).
- Project branding assets (`assets/branding/`: logo, wordmark, icons)
  and a branded README.

### Known limitations
- Addon **execution** is not implemented; only manifest
  discovery/validation exists. No safe process-isolation mechanism is
  available in the reference environment.
- No Jev (Decision Intelligence) adapter exists yet.
- GPU/local-compute telemetry is a live snapshot only, not attributed
  to individual completed tasks.
- The dashboard is plain-text, not a curses-based interactive TUI.
- `route-and-run` does not currently go through the same risk-based
  Human Approval gate that `run-task` does.

See `docs/specification/` for the full behavioral specification and
`docs/specification/10_Roadmap/IMPLEMENTATION_ROADMAP_v0.4.md` for the
phase-by-phase roadmap this release implements.
