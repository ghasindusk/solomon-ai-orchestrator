# Solomon v0.4 Implementation Roadmap

Progress: 0% implementation

## Phase 0 - Environment Discovery
- [ ] Inspect actual LocalAI/Ollama installation/API/models
- [ ] Inspect Claude Code CLI/non-interactive behavior/output/usage
- [ ] Inspect Codex CLI/non-interactive behavior/output/usage
- [ ] Inspect Antigravity integration points
- [ ] Inspect current Obsidian/LocalAI retrieval flow
- [ ] Establish version/capability registry

## Phase 1 - Runtime Foundation
- [ ] Project registry
- [ ] Goal/task state machines
- [ ] SQLite event/state store
- [ ] Policy engine
- [ ] Definition of Done
- [ ] Checkpoint/resume
- [ ] Git checkpoint/rollback

## Phase 2 - Invisible Gateway
- [ ] Invocation envelope
- [ ] Claude Code gateway
- [ ] Codex gateway
- [ ] Delegation gate
- [ ] Return-to-caller
- [ ] Shadow mode

## Phase 3 - Knowledge & Context
- [ ] Obsidian/LocalAI provider
- [ ] Project/global knowledge scopes
- [ ] authority/supersession
- [ ] Context Firewall
- [ ] context compression/savings metrics

## Phase 4 - Routing & Execution
- [ ] capability discovery
- [ ] dynamic role assignment
- [ ] router scoring
- [ ] structured task results
- [ ] remediation/review tasks
- [ ] locks/worktrees
- [ ] safe parallel execution

## Phase 5 - Extensions
- [ ] Addon Manager
- [ ] Addon manifest validation
- [ ] permission system
- [ ] MCP tool bus
- [ ] A2A transport
- [ ] optional Jev adapter

## Phase 6 - Token & Compute Intelligence (renamed from Usage Intelligence)
- [ ] common UsageRecord schema, implemented and version-managed
- [ ] per-adapter token value + provenance capture (Claude Code, Codex, Antigravity, LocalAI/Ollama, Jev)
- [ ] UNKNOWN vs 0 kept distinct throughout storage/display
- [ ] Project / Agent / Goal / Task aggregation
- [ ] Context Saved measurement (raw/sent/saved/reduction_ratio)
- [ ] Token Budget (Global/Project/Agent/Model/Goal/Task) wired to warning/suppression/fallback/Human Approval policy
- [ ] Token Efficiency exposed as an optional Historical Router auxiliary signal
- [ ] Event Store <-> UsageRecord cross-reference
- [ ] tests for double-counting, mislabeled estimates, cross-project leakage
- [ ] Core fully functional with no Cost Calculator addon present
- [ ] Cost Calculator boundary respected: no currency/pricing logic required in Core

## Phase 7 - Reliability & Evaluation
- [ ] replay
- [ ] eval suite
- [ ] recovery tests
- [ ] context-leak tests
- [ ] addon isolation tests
- [ ] router comparison

## Phase 8 - UX
- [ ] inspect/status CLI
- [ ] TUI/dashboard
- [ ] autonomy dial
- [ ] diagnostics export

## Phase 9 - GitHub Alpha
- [ ] README
- [ ] LICENSE decision
- [ ] CONTRIBUTING
- [ ] SECURITY
- [ ] CODE_OF_CONDUCT
- [ ] issue/discussion templates
- [ ] branding assets
- [ ] v0.4.0-alpha release
