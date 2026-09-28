# Octavryn SI

*Symbiotic Intelligence Orchestration System. An extensible orchestration
system for human and machine intelligence.*

**Status: v0.6.0-alpha.1 — Phase 1A preview.** Octavryn SI was
originally released as Solomon AI Orchestrator v0.4.0-alpha. "SI" means
*Symbiotic Intelligence*: no AGI or superintelligence claim is made.

Octavryn is a local, invisible multi-AI orchestration runtime. You keep
working in your normal AI interface (Claude Code, Codex, ...); a gateway
can delegate suitable requests to Octavryn in the background. Octavryn
reconstructs project context, decomposes the goal into tasks, routes each
task to the best-suited available AI agent, verifies the result against
a real Definition of Done, records evidence, and returns the integrated
result to the original caller.

Octavryn is **not** an AI-to-AI chat system. The default communication
mode between agents is structured task/result contracts, not free-form
dialogue. A bounded, opt-in Debate Mode exists for a small set of
high-value design decisions only.

## Core principles

- **Invisible orchestration** -- no need to explicitly "call Octavryn".
- **Role != Agent** -- roles (Implementer, Reviewer, Tester, ...) are
  selected first; which AI provider fills that role is decided
  dynamically, based on real historical performance and current
  availability.
- **Agent output is evidence, not completion** -- a task only reaches
  COMPLETE after Octavryn verifies it against a Definition of Done, never
  from an agent's own claim.
- **Safety above autonomy** -- policy and human-approval gates cannot be
  overridden by an agent, an addon, or a lower autonomy setting.
- **Token & Compute Intelligence, not cost accounting** -- Octavryn's core
  usage model is tokens and compute; pricing/currency is explicitly out
  of Core's required scope (a future Cost Calculator addon can layer
  that on top of the recorded UsageRecords).
- **Local-first, privacy-first** -- telemetry is off by default, and
  diagnostics exports are redacted by default (see `octavryn
  diagnostics-export --help`).
- **Durable and observable** -- every meaningful action is an event;
  state survives interruption and can be reconciled/resumed.

## Status

This is an alpha-stage research/personal project, not a production
release. The core runtime (routing, execution, verification, recovery,
Token & Compute Intelligence, replay/eval tooling, diagnostics export) is
implemented and covered by an automated regression suite. Some areas are
intentionally thin or deferred. See the changelog and the public
specification under `docs/specification/` for supported behavior and limits.

Known, intentional scope limits (not bugs):
- Addon execution (`ENABLED` state) is not implemented -- addon
  manifests are discovered and validated only. This environment has no
  process-isolation mechanism safe enough to run third-party addon code.
- A Jev (Decision Intelligence) adapter does not exist yet; the data
  model that would record its usage is in place and unused.
- The dashboard is a plain-text, periodically-refreshing CLI view, not a
  curses-based interactive TUI (deliberate, to avoid a
  platform-specific dependency).

## Requirements

- Python 3.11+
- See `requirements.txt` for Python dependencies (`pyyaml`, `mcp`).
- At least one supported AI CLI adapter available on your machine
  (Claude Code, Codex, Antigravity, Orca, or a local Ollama install) --
  Octavryn orchestrates existing tools, it doesn't replace them. Orca is an
  optional execution-plane integration; direct adapters remain supported.

## Getting started

```bash
pip install -r requirements.txt

# Copy the example configs and fill in your own values -- the real
# files are gitignored (they hold your local project paths).
cp 04_Config_Schemas/projects.registry.example.yaml 04_Config_Schemas/projects.registry.yaml
cp 03_Policies/GLOBAL_POLICY.example.yaml 03_Policies/GLOBAL_POLICY.yaml

# List CLI subcommands
python -m octavryn --help
```

A few commands to try once you've registered a project:

```bash
python -m octavryn project-list
python -m octavryn route --role coder --project-id your_project
python -m octavryn dashboard --project-id your_project
python -m octavryn diagnostics-export --project-id your_project
```

`python -m octavryn --help` lists every subcommand (routing, execution,
approvals, worktree-isolated parallel runs, usage/budget inspection,
replay, router comparison, the categorized eval runner, and more).

## Migrating from Solomon (v0.4)

Nothing breaks on upgrade. The old names keep working as deprecated
aliases through the v0.6 alpha line:

| v0.4 | v0.6 canonical | Compatibility |
|---|---|---|
| `solomon` / `python -m solomon.cli` | `octavryn` / `python -m octavryn` | alias, one-line notice on stderr |
| MCP server `solomon` (`python -m solomon.mcp_server`) | `octavryn` (`python -m octavryn.mcp_server`) | alias serves the same read-only tools; `octavryn mcp-migrate status\|apply\|rollback` moves client registrations via the official client CLI |
| `state/solomon.sqlite3` | `state/octavryn.sqlite3` | `octavryn migrate state [--apply]` copies with backup + verification; `octavryn migrate rollback` |
| `solomon_compatibility` / `solomon-addon.yaml` | `octavryn_compatibility` / `octavryn-addon.yaml` | old key/file still read |
| gateway mode `FORCE_SOLOMON` | `FORCE_OCTAVRYN` | old value still accepted |
| `import solomon.x` | `import octavryn.x` | same module objects (the implementation package remains `solomon` during v0.6 Phase 1A) |

## Orca execution-plane integration

v0.6 can use **Orca** as an optional supervised execution plane while Octavryn
remains the control plane. Octavryn keeps ownership of routing, memory/RAG,
governance, approvals, capability evidence, and Definition-of-Done
verification; Orca owns supervised agent/worktree lifecycle for delegated
tasks.

The first integration slice is deliberately conservative: it uses the current
Orca-managed worktree, accepts a configurable Orca agent, and only returns
RESULT_RECEIVED after a matching worker_done task/dispatch receipt. Questions,
escalations, unknown lifecycle state, and mismatched IDs fail closed rather
than being inferred as success.

See docs/specification/02_Architecture/Octavryn_Orca_Execution_Plane_v0.6.md
for the boundary, rollout plan, and current limitations.

## Running the test suite

```bash
python -m pytest -q
```

## Documentation

- `docs/specification/01_Specification/` -- the normative v0.4 behavior spec.
- `docs/specification/02_Architecture/` -- runtime topology and component design.
- `docs/specification/10_Roadmap/` -- the published implementation roadmap.
- `03_Policies/`, `04_Config_Schemas/` -- policy and config examples.
- `examples/v06_context/` -- Phase 1A context-contract examples and fixtures.
- `CHANGELOG.md` -- public release history.

## Contributing

See `CONTRIBUTING.md`. Please read `SECURITY.md` before reporting a
security-relevant issue.

## License

MIT -- see `LICENSE`.
