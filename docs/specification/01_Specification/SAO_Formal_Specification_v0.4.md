# Solomon AI Orchestrator - Formal Specification v0.4

Progress: 100% design baseline

## 1. Product Definition
Solomon is a local orchestration runtime that coordinates multiple AI coding/automation agents toward a user goal while preserving the user's existing workflow. The normal user experience begins in an existing caller such as Codex or Claude Code. The caller/gateway may delegate the request to Solomon automatically.

## 2. Primary UX: Invisible Orchestration
The user does not need to invoke or address Solomon explicitly.

Example:
User -> "Create this patch and validate it."
Caller AI -> Solomon Gateway -> Solomon Runtime -> Tasks/Agents -> Verification -> Original Caller -> User.

A dedicated Solomon CLI/TUI remains available for administration, inspection, replay, diagnostics and explicit control, but is not the primary daily interaction surface.

## 3. Delegation Gate
The gateway determines whether a request benefits from orchestration.
Simple/local requests may remain with the caller. Multi-step, cross-file, historical-context, review/test, multi-agent or high-risk requests may be delegated.
Delegation must be inspectable and configurable. Shadow mode may compare delegation decisions without taking control.

## 4. Goal and Task Model
Solomon converts a delegated request into:
Goal -> dependency-aware Tasks -> required Roles -> candidate Agents -> execution -> structured results -> verification/remediation -> completion.

## 5. Role/Agent Separation
Roles include Planner, Architect, Implementer, Debugger, Reviewer, Tester, Researcher, Documenter, Environment Operator, Knowledge Curator and custom addon roles.
Agents are providers/runtimes. Roles are not permanently bound to agents.

## 6. Agent Communication
Default: no free AI-to-AI conversation.
All handoffs use structured task/result contracts through Solomon.
Bounded Debate Mode is optional for selected design decisions only, with max agents, rounds, time, token budget and termination/judge rules.

## 7. Knowledge and Context
LocalAI/Ollama may query Obsidian and other registered knowledge providers.
Memory layers:
L0 raw knowledge
L1 retrieval index
L2 project memory
L3 authoritative decision memory
L4 working task memory
L5 minimal agent context

A Context Firewall filters relevance, duplicates, obsolete/superseded data, secrets, project-boundary violations and token excess before context is sent to an execution agent.

## 8. Dynamic Routing
Routing considers skill match, project experience, historical quality, context fitness, availability, privacy, speed, usage pressure, cost and failure risk.
Jev may optionally provide typed decision/confidence signals. Hard policy and deterministic safety rules always outrank Jev or statistical routing.

## 9. Durable Execution
Goal/task state is persisted after meaningful transitions. Interrupted work can resume without repeating completed tasks unless evidence is stale or invalid.
Suggested states:
Goal: CREATED, PLANNED, RUNNING, WAITING_APPROVAL, VERIFYING, BLOCKED, COMPLETE, FAILED, CANCELLED.
Task: QUEUED, READY, ASSIGNED, RUNNING, RESULT_RECEIVED, VERIFYING, REMEDIATION_REQUIRED, BLOCKED, COMPLETE, FAILED, CANCELLED.

## 10. Event Sourcing and Observability
Every meaningful action produces an event. Events are the canonical audit trail; human-readable project/agent logs are derived views.
Examples: GOAL_CREATED, TASK_CREATED, ROUTE_SELECTED, AGENT_STARTED, FILE_CHANGED, TEST_FAILED, REMEDIATION_CREATED, TEST_PASSED, REVIEW_PASSED, TASK_COMPLETED.

## 11. Dual Log Views
Maintain both:
- Project-centric logs: what happened in a project/goal/task.
- Agent-centric logs: what each AI did across projects.
Avoid unnecessary content duplication by referencing canonical event IDs/artifacts.

## 12. Project Isolation
Each project has independent state, documents, materials, logs, tests and artifacts. Global policies/knowledge are separate.
Cross-project context requires explicit policy permission.

## 13. Documentation Rules
For user-directed project documents:
- display progress percentage,
- maintain TODO checklist,
- preserve revision history/diffs,
- record timestamp,
- record why a meaningful change occurred,
- record agent/reviewer/test evidence where applicable.
Historical decisions are superseded, not silently erased.

## 14. Definition of Done
Agent self-declaration never completes a task. Solomon verifies applicable requirements such as result, diff, build, tests, review, docs, TODO/progress, decision record and version-control evidence.

## 15. Safety and Approval
Suggested levels:
L0 read/analysis - automatic.
L1 create files in project - automatic.
L2 code modification - automatic with checkpoint/rollback.
L3 broad deletion/large migration - human approval.
L4 privileged OS/external publication/high-impact operation - explicit human approval.
Addon permissions and autonomy never override higher-level safety.

## 16. Autonomy Dial
0 Proposal only.
1 Research/analysis.
2 Safe changes.
3 Implementation + test + review.
4 Continue until goal completion.
5 Ongoing project management.
Project defaults may differ. Safety policy always overrides autonomy.

## 17. Usage Management (Token & Compute Intelligence)
Core's common usage baseline is Token and Compute, not currency. Pricing/cost is explicitly out of Core's required responsibility; a future Cost Calculator addon reads UsageRecord and applies a provider pricing DB and currency rules.

### 17.1 Principles
- Core's primary usage metrics are Token and Compute.
- Pricing/currency/provider price tables are never required for Core routing.
- Measured, calculated, estimated and unknown values are kept distinct.
- A value that cannot be obtained is stored as UNKNOWN, never coerced to 0.
- Estimated values are never displayed merged with measured values as an "exact" total.
- Usage is aggregatable by Agent, Model, Project, Goal, Task, Session, day and month, not only Agent.
- Token efficiency is an auxiliary routing signal only, never ranked above quality or safety.

### 17.2 UsageRecord schema (field categories)
- Identity: `timestamp`, `project_id`, `goal_id`, `task_id`, `session_id` - binds usage to a unit of work.
- Agent: `agent_id`, `provider`, `model`, `role`.
- Tokens: `input`, `output`, `cached_input`, `context`, `total` - as reported by the provider.
- Savings: `raw_context`, `sent_context`, `context_saved` - reduction from the Context Firewall and similar stages.
- Compute: `duration_ms`, `queue_ms`, `tokens_per_second`.
- Local Compute: `gpu_time`, `vram_peak`, `ram_peak` - local inference resources, where obtainable.
- Decision: `decision_count` - Jev-style decision invocations.
- Quality: `success`, `retry_count`, `review_result`, `test_result` - for efficiency analysis.
- Provenance: one of API_REPORTED, CLI_REPORTED, CALCULATED, ESTIMATED, UNKNOWN.

### 17.3 Provenance priority
API_REPORTED (direct provider/API value) > CLI_REPORTED (official CLI-reported value) > CALCULATED (derived by Solomon from a trusted tokenizer/confirmed data) > ESTIMATED (derived from context; UI must mark it as an estimate) > UNKNOWN (no reliable source; never converted to 0).

### 17.4 Aggregation axes
Filter/aggregate along Agent -> Model -> Project -> Goal -> Task -> Session -> Day -> Month. Project-level and Agent-level usage views share the same IDs as the Dual Log/Event Store and reference the same underlying records from different views.

### 17.5 Context Savings
Context Firewall, RAG, summarization and de-duplication reductions are measured as `raw_context_tokens`, `sent_context_tokens`, `context_saved_tokens`, `reduction_ratio`, evaluable per Project/Goal/Agent.

### 17.6 Token Efficiency
The Historical Router may use Token Efficiency (`tokens_per_task`, `tokens_per_success`, `tokens_per_verified_success`, `retry_token_overhead`) as an auxiliary signal. It must not mechanically select the lowest-token agent; efficiency is compared only among candidates that already satisfy Safety Policy, required capability, quality and project experience.

### 17.7 Jev (Decision Intelligence)
Jev is tracked separately from generation agents as Decision Intelligence: `input_tokens` plus `decision_count`, `average_input_tokens_per_decision`, and Decision Profile results. Jev usage is governed by a Token Budget rather than currency; on budget exhaustion it must fall back safely to Rule/Historical Router.

### 17.8 Token Budget
Budgets can be set at Global / Project / Agent / Model / Goal / Task scope and connect to warning, routing suppression, fallback and Human Approval policy. Budget exhaustion must never be used to skip required Safety/Verification; if required Review/Test cannot run, the Task transitions to BLOCKED or WAITING_APPROVAL instead.

### 17.9 Token Flow
Token flow through Gateway, Knowledge Retrieval, Context Firewall, Planner, execution agents, Jev and Review is traceable via Event/Usage IDs within a Goal, for future Dashboard/TUI visualization (Goal Total, per-agent breakdown, Context Saved, Retry Overhead, Decision Tokens).

Extended source: `Solomon_AI_Orchestrator_v0.4_Additional_Spec_Token_Compute_Intelligence.docx` (2026-09-23), treated as an addendum to this Formal Specification.

## 18. Shadow, Replay and Eval
Shadow: evaluate Solomon decisions without taking control.
Replay: re-run historical event/task inputs against a new router/policy without mutating the original record.
Eval: deterministic and integration suites for routing, context firewall, safety, recovery, addons and completion semantics.

## 19. MCP and A2A
MCP is the preferred tool integration bus where appropriate.
A2A support is an optional external-agent transport. Solomon uses it for structured task/result exchange, not unrestricted agent chat.

## 20. Addon Platform
Third-party addons can extend agents, tools, MCP servers, A2A endpoints, roles, task types, routing strategies, knowledge providers, context processors, validators, reviewers, project templates, policy packs, dashboard extensions, event hooks and telemetry providers.
Addons use versioned manifests, declared permissions and compatibility constraints. Core safety policy is not overrideable.

## 21. Privacy and Public Distribution
Telemetry is disabled by default.
Diagnostics exports must redact/omit prompts, source contents, secrets, private paths and Obsidian contents unless the user explicitly includes them.
GitHub publication must use example configuration and never contain local secrets or personal paths.

## 22. Extensibility
Core provider-specific logic lives behind adapters. Missing optional providers do not prevent Solomon from starting if a viable execution path remains.
