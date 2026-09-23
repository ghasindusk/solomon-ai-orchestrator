# Logging, Event Store & Project Directory Standard v0.4

## 1. Canonical event store
The canonical audit trail is append-oriented event data stored in the runtime database and/or JSONL export. Human-readable logs are views derived from these events.

## 2. Project log view
logs/projects/<project-id>/<YYYY>/<MM>/<DD>/<goal-id>/
  GOAL.md
  TASKS.md
  SUMMARY.md
  EVENTS.jsonl
  DIFF.md
  TESTS.md
  REVIEW.md
  USAGE.json
  artifacts/

## 3. Agent log view
logs/agents/<agent-id>/<YYYY>/<MM>/<DD>.jsonl
Each entry references project_id, goal_id, task_id and canonical event_id.

## 4. Project workspace standard
projects/<project-id>/
  .solomon/
    PROJECT.yaml
    STATE.json
    TASKS.json
    LOCKS.json
  docs/
    specifications/
    architecture/
    research/
    decisions/
    guides/
    changelog/
    archive/
  materials/
    references/
    analysis/
    patches/
    generated/
  logs/
  tests/
  artifacts/

## 5. Global knowledge
knowledge/global/
  policies/
  coding-standards/
  security/
  shared-guides/

knowledge/projects/<project-id>/

## 6. Required task record
- timestamp
- project_id
- goal_id
- task_id
- role
- agent
- model when known
- caller
- context sources
- files read/modified
- result
- diff/change reference
- test/review result
- retries
- usage + provenance
- git/checkpoint state
- errors/uncertainties

## 7. Revision policy
Do not silently overwrite meaningful historical decisions. Create a new decision/change event and mark the old record superseded where appropriate.

## 8. Document convention
# Document Title
Progress: NN%
## TODO
- [x] completed
- [ ] pending

Meaningful revisions record what changed, why, agent, reviewer/test evidence and timestamp.
