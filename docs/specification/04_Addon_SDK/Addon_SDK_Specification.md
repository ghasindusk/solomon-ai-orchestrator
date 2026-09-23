# Solomon Addon SDK Specification v0.4

Progress: 100% interface baseline

## Goals
Enable community extensions without patching Solomon Core.

## Addon package
addon-name/
  solomon-addon.yaml
  README.md
  src/
  policies/
  prompts/
  schemas/
  tests/

## Extension points
- Agent Adapter
- Tool
- MCP Server registration
- A2A external agent
- Role
- Task Type
- Router Strategy
- Knowledge Provider
- Context Processor
- Validator
- Reviewer
- Project Template
- Policy Pack (lower priority than core safety/global constraints)
- Dashboard Extension
- Event Hook
- Telemetry Provider

## Lifecycle
DISCOVERED -> VALIDATED -> PERMISSION_REVIEW -> ENABLED -> DISABLED/QUARANTINED.

## Permission model
Example permissions:
project.read
project.write
git.read
git.write
shell.execute
network.connect
knowledge.read
telemetry.emit
filesystem.outside_project
secrets.use

Sensitive permissions require explicit consent. Addons cannot request or override core safety authority.

## Compatibility
Manifest declares addon API version and compatible Solomon versions.
Solomon must fail closed for incompatible addon APIs.

## Isolation
Initial implementation may use process isolation for untrusted/high-risk addons. Future sandboxing can strengthen this.
Addon crashes must not corrupt the core event/state store.

## Distribution
Phase 1: Git repository/local folder install.
Future: signed registry/catalog.
