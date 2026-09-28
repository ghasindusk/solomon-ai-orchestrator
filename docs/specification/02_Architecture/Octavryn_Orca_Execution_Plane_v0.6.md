# Octavryn SI + Orca Execution Plane

Status: **v0.6 pilot / feature branch**

## Decision

Octavryn remains the **control plane**. Orca is integrated as a managed
**execution plane** beneath the existing provider-independent adapter contract.

This is an integration, not a replacement:

- Octavryn owns project context, LocalAI/Ollama RAG, routing, capability
  evidence, governance, approval gates, Definition-of-Done verification,
  durable task state, usage policy, and specialist-system selection.
- Orca owns supervised agent process/worktree execution and the authoritative
  worker lifecycle for tasks delegated through the Orca execution backend.
- Claude Code, Codex, Antigravity, and local Ollama remain the routing
  identities. Orca is not a new intelligence candidate and is not mandatory.

## Why this boundary

Orca already provides a mature supervised worker lifecycle around coding
agents. Reimplementing terminal lifecycle, agent boot, worktree placement,
dispatch provenance, and worker completion inside Octavryn would duplicate
infrastructure while increasing failure modes.

Octavryn still provides the system-level functions Orca is not being asked to
own: cross-project memory/RAG, provider-independent routing, policy and human
approval, capability evidence, specialist tools such as AIS, and final
verification.

## Pilot execution contract

The v0.6 pilot implements solomon.adapters.orca_adapter.OrcaAdapter as an
execution backend selected by per-project policy. The logical adapter identity
is preserved: if Octavryn routes a task to codex, TaskResult.agent remains
codex even when Orca owns the supervised worker lifecycle.

A delegated execution follows this supervised sequence:

1. orca status --json
2. Create a dedicated Orca coordinator terminal in the active worktree and
   retain its authoritative terminal handle.
3. orca orchestration run-create --objective ... --from <coordinator> --json
4. Read the authoritative Run ID from the receipt.
5. orca orchestration worker-start --spec ... --worktree current --agent ...
   --run <run_id> --from <coordinator> --json
6. Consume with orca orchestration check --terminal <coordinator>
   --run <run_id> --wait --types worker_done,escalation,question ... --json
7. Validate worker_done.taskId **and** worker_done.dispatchId against the
   authoritative worker-start receipt.
8. After a settled worker, call worker-release. A failed or unverified release
   leaves the Delivery unacknowledged.
9. Only after confirmed release, acknowledge the Delivery through the same
   coordinator terminal and Run. Close Octavryn's coordinator terminal only
   after a confirmed ACK.
10. Convert the worker outcome into an Octavryn TaskResult.
11. Octavryn still performs its own Definition-of-Done verification before a
    task can become COMPLETE.

worker_done is execution evidence, not final Octavryn completion.

## Safety invariants

The adapter intentionally fails closed:

- No matching taskId + dispatchId -> no success.
- Unknown/missing worker outcome -> no success.
- question or escalation -> return explicit coordinator action required;
  do not fabricate an answer and do not acknowledge the delivery.
- Failed worker-start -> do not automatically relaunch. The original dispatch
  may have residual resources or uncertain authority.
- Wait-window expiry -> do not stop or retry the worker. Report the dispatch as
  unverifiable and preserve its IDs for inspection.
- A failed/unverified worker-release is a recovery condition: the Delivery is
  not acknowledged and the coordinator terminal is retained.
- An ACK failure is recorded as uncertainty and the coordinator terminal is
  retained so the Delivery can be replayed/repaired.
- The adapter invokes Orca directly with shell=False. Arbitrary task prompts
  never pass through a command shell.

## Pilot placement limit

The first slice supports this project execution profile:

    execution_profile:
      codex:
        execution_backend: orca
        orca:
          worktree: current

The calling project's directory therefore needs to resolve to an Orca-managed
current worktree. Octavryn creates its own short-lived coordinator shell
terminal in that worktree because the Python process is outside Orca's
interactive terminal context. Automatic repo registration and creation of
top-level/child Orca worktrees are deliberately deferred until
placement/recovery semantics are covered by dedicated tests.

This avoids silently duplicating Octavryn's current WorktreeManager during the
migration.

## Responsibility matrix

| Concern | Octavryn | Orca |
|---|---|---|
| User goal intake | Owner | - |
| Cross-project memory / RAG | Owner | Consumer via task context |
| Capability registry | Owner | Execution capability source |
| Provider/agent routing | Owner | Executes selected worker |
| Policy / approvals | Owner | Must not widen |
| Task decomposition | Owner | Optional nested execution later |
| Terminal / agent lifecycle | Observes | Owner |
| Worktree execution | Policy owner | Execution owner |
| worker_done provenance | Validates | Produces |
| Definition of Done | Owner | Supplies evidence |
| Final COMPLETE state | Owner | Never decides |
| Usage/token budget | Owner | Telemetry bridge deferred |
| Remote worker runtime | Policy owner | Execution owner when enabled |

## Migration plan

### Slice A - supervised execution backend (this branch)

- Add OrcaAdapter as a backend implementation.
- Keep Orca out of the intelligence/routing registry.
- Let per-project policy wrap selected Codex or Claude Code execution in Orca.
- Preserve the selected logical provider name in TaskResult and history.
- Add lifecycle/fail-closed unit tests, including explicit Run/coordinator
  scoping and release-before-ACK ordering.
- Document control-plane/execution-plane boundary.
- Keep every existing execution path intact.

### Slice B - placement broker

- Discover the exact Orca repo/worktree identity for an Octavryn project.
- Add tested top-level/child worktree placement.
- Preserve Octavryn approval and path-policy checks before worktree creation.
- Add recovery receipts for residual resources; never blind-retry a dispatch.

### Slice C - observability and usage

- Map Orca Run/Task/Dispatch IDs into Octavryn evidence/provenance records.
- Ingest provider usage only when Orca exposes authoritative values; otherwise
  keep token fields UNKNOWN.
- Surface Orca liveness/attention state in Octavryn diagnostics without
  conflating liveness with completion.

### Slice D - de-duplicate native execution infrastructure

Only after real-world acceptance and regression coverage:

- Prefer Orca for agent terminal/worktree lifecycle where available.
- Retain Octavryn's native WorktreeManager as a fallback until migration is
  demonstrably safe.
- Evaluate whether Octavryn's remote-execution implementation can become a
  compatibility/fallback layer instead of a primary path.
- Do **not** remove routing, governance, approval, memory/RAG, verification,
  audit, or specialist-tool orchestration.

## External contract used

The integration follows Orca's current supervised orchestration guidance:

https://github.com/stablyai/orca/blob/main/skill-guides/orchestration.md

That contract explicitly distinguishes worker liveness from completion and
requires lifecycle IDs on worker_done. Octavryn mirrors that rule rather than
inferring success from terminal idle state or process output.
