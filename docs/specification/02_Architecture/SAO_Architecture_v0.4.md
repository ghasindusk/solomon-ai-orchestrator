# Architecture Design v0.4

Progress: 100% design baseline

## Runtime topology
User
 -> Caller AI (Codex / Claude Code / other)
 -> Solomon Gateway
 -> Delegation Gate
 -> Solomon Runtime
    - Director
    - Project Manager
    - Planner
    - Policy Engine
    - State Engine
    - Knowledge Manager
    - Context Firewall
    - Decision Engine
    - Optional Jev Adapter
    - Dynamic Router
    - Usage Manager (Token & Compute Intelligence; emits UsageRecord, no pricing/currency logic)
    - Event Store
    - Execution Engine
    - Verification Engine
    - Addon Manager
 -> Agent Adapters / MCP / A2A
 -> Structured Results
 -> Verification / remediation
 -> state + logs + docs + knowledge update
 -> Original Caller

## Core storage
Recommended:
- SQLite for runtime/event/state indexes.
- JSON/JSONL for portable structured exports.
- Markdown for human-facing logs/specs/decisions.
- Git for source checkpoints and project history.

## Routing decision
Hard constraints first:
1. Safety/policy eligibility
2. Required capability
3. Project/workspace access
4. Availability
Then rank eligible candidates using configured weighted factors.
Optional Jev signals can modify ranking within policy bounds.

## Recovery
Persist before and after external agent execution. Every task invocation gets an idempotency key where feasible. On restart, reconcile RUNNING tasks against process/provider state and mark as resumable, retryable, unknown or failed.

## Concurrency
Read-only tasks can run concurrently.
Write tasks require file/path locks or isolated worktrees.
Conflicting write sets cannot merge without verification.

## Caller identity
Track:
caller_type, caller_session_id (when available), return_channel, project_hint, working_directory, invocation_id.
Caller is not necessarily Executor.
