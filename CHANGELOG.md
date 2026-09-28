# Changelog

All notable changes to Octavryn SI (formerly Solomon AI Orchestrator) are
documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This is a pre-1.0 alpha. Behavior, CLI flags, and schemas may change
without a deprecation period until v1.0.0.

## [Unreleased]

### Added
- Optional Orca supervised execution-plane adapter. Octavryn remains the
  control plane for routing, memory/RAG, governance, approval and final
  Definition-of-Done verification.
- Orca lifecycle tests for authoritative task/dispatch IDs, worker-reported
  failure, questions/escalations, cleanup ordering and no blind retry.

### Security
- Orca worker output is accepted only from a matching worker_done taskId and
  dispatchId. Unknown lifecycle state fails closed and is never promoted to
  task completion.
- Invalid adapter execution profiles now degrade to an unavailable adapter
  instead of crashing provider loading.

## [0.6.0-alpha.1] - 2026-09-27

Phase 1A preview. Provider adapters and automatic Source of Truth merge are
not enabled by this release.

### Added
- Provider-independent, immutable context contracts for intake, evidence,
  proposals, knowledge revisions, outcome contracts, verification reports,
  and additive Memory migration manifests.
- Project-scoped SQLite persistence with operation replay, proposal
  compare-and-swap transitions, canonical JSON, and bundled JSON Schemas.
- Adversarial fixtures and validation for project isolation, lifecycle,
  schema drift, immutable records, and explicit `UNKNOWN` outcomes.

### Security
- Fail closed on unsupported schema-ledger versions, altered table DDL,
  unexpected triggers, stored-key/record-ID mismatches, evidence-ID
  mismatches, control characters, and attempts to shadow record metadata.
- Preserve legacy Solomon compatibility; this preview performs no automatic
  provider ingestion, approval merge, live credential use, or public endpoint.

## [0.5.0-alpha] - unreleased

Octavryn SI was originally released as Solomon AI Orchestrator v0.4.0-alpha.
"SI" means Symbiotic Intelligence. No AGI or superintelligence claim is made.

### Added
- Provider-independent core: Intelligence Registry with read-only discovery,
  Capability Graph with evidence states (declared / historically verified /
  user-confirmed), capability-first routing through a Skill Registry with
  scoped, versioned Skill Packs (install/uninstall, no permission escalation).
- Formal adapter contract with explicit `Unsupported` results; an adapter
  registry that keeps the core working when any named provider is absent.
- Unified governance: run-task, route-and-run, run-batch, debate and
  review-task all pass the same gates. Approvals are bound to the exact action
  (hash), expire, are single-use, and carry the context a human needs.
- Evaluation records for every execution.
- Optional desktop surfaces (Claude Desktop / ChatGPT Desktop), detected
  read-only and never granted execution authority.
- Remote control foundation (in-process, no network listener): signed task
  envelopes, replay/expiry/scope checks, worker state and durable checkpoints,
  offline queue, revocation, kill switch.
- Publication tooling: allowlisted candidate build with a fail-closed privacy scan.

### Changed
- `octavryn` is the canonical CLI and MCP server name. `solomon` keeps working
  as a deprecated alias (stderr notice) through the v0.5 alpha line.
- State store migrates copy-based to `state/octavryn.sqlite3`
  (`octavryn migrate state --apply`, with backup, verification and rollback).
- MCP `list_approvals` redacts credential-like strings.

### Security
- Wider secret detection (dash-containing `sk-` keys, `github_pat_`, `AIza`, `xox*`).

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
