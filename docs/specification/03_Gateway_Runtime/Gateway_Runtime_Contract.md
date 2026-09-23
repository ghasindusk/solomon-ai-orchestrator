# Gateway & Runtime Contract v0.4

## Principle
The user normally talks to the caller AI, not Solomon.

## Gateway responsibilities
1. Observe/receive a user request.
2. Determine whether orchestration is useful.
3. Create a normalized invocation envelope.
4. Submit it to Solomon.
5. Receive progress/final result.
6. Return the integrated result to the original caller/session.

## Invocation envelope
Required:
- invocation_id
- caller_type
- request
- timestamp
Optional:
- caller_session_id
- project_hint
- working_directory
- autonomy_override
- privacy_constraints
- expected_output

## Delegation policy
Examples likely to delegate:
- multi-step implementation
- cross-file changes
- project-history dependent work
- implementation + test + review
- multiple specialist roles
- long-running tasks
- explicit project goal continuation

Examples likely to remain local:
- tiny explanation
- isolated wording question
- trivial one-file edit where caller can safely complete it

## Modes
AUTO: gateway chooses.
FORCE_LOCAL: caller handles.
FORCE_SOLOMON: delegate.
SHADOW: Solomon plans/scores but does not control execution.

## Return contract
- goal_id
- status
- summary
- tasks summary
- changed artifacts
- verification state
- approvals/blockers
- usage summary with provenance
- log/event references
