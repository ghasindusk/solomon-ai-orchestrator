"""Adapter registry (Octavryn SI v0.5 spec 05, roadmap R4).

Replaces the hardcoded if/elif chain that used to live in cli._load_adapter.
Core code looks adapters up by name through here and never imports a
provider module directly, so Core keeps working when any named adapter
is missing ("Core MUST remain functional when any named adapter is
absent").

Built-in integrations are imported lazily. If an integration module is
missing or fails to import, load_adapter() returns a MissingAdapter.
Its health() is always unavailable and its execute() returns FAILED with
a clear reason. Routing and fallback treat it like any other unavailable
adapter instead of crashing.

Registration is programmatic only (register_adapter). Loading arbitrary
`module:Class` strings from config would let a config file import code,
which would be a trust escalation (spec 01 "No trust escalation").
"""

from __future__ import annotations

import importlib
from typing import Callable

from ..models import Task
from ..result import TaskResult
from .base import AdapterHealth, AgentAdapter, Unsupported

# name -> (module path relative to this package, class name, accepts cwd?)
_BUILTINS: dict[str, tuple[str, str, bool]] = {
    "claude_code": (".claude_code", "ClaudeCodeAdapter", True),
    "codex": (".codex_adapter", "CodexAdapter", True),
    "localai_ollama": (".ollama_adapter", "OllamaAdapter", False),
    "antigravity": (".antigravity_adapter", "AntigravityAdapter", True),
}

_CUSTOM: dict[str, Callable[..., AgentAdapter]] = {}
_DISABLED: set[str] = set()


class UnknownAdapterError(ValueError):
    pass


class MissingAdapter(AgentAdapter):
    """Stand-in for a registered integration whose code cannot be loaded
    (or that was disabled for a missing-provider test)."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason

    def health(self) -> AdapterHealth:
        return AdapterHealth(False, f"adapter '{self.name}' absent: {self.reason}")

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        now = self._now()
        return TaskResult(
            task_id=task.task_id,
            status="FAILED",
            summary=f"adapter '{self.name}' is absent",
            agent=self.name,
            started_at=now,
            finished_at=now,
            uncertainties=[f"unavailable: adapter absent ({self.reason})"],
        )

    def discover(self):
        return Unsupported("discover", self.name, f"adapter absent: {self.reason}")


def register_adapter(name: str, factory: Callable[..., AgentAdapter]) -> None:
    """factory(cwd=None) -> AgentAdapter. Used by tests and future
    integrations shipped as Python code the user installed on purpose."""
    _CUSTOM[name] = factory


def unregister_adapter(name: str) -> None:
    _CUSTOM.pop(name, None)


def set_disabled(names: set[str] | list[str]) -> None:
    """Simulate named providers being absent (roadmap R8 matrix tests,
    or a user who wants to exclude a provider entirely)."""
    _DISABLED.clear()
    _DISABLED.update(names)


def known_adapter_names() -> list[str]:
    return sorted(set(_BUILTINS) | set(_CUSTOM))


def load_adapter(name: str, cwd: str | None = None) -> AgentAdapter:
    if name in _DISABLED:
        return MissingAdapter(name, "disabled by configuration")
    if name in _CUSTOM:
        factory = _CUSTOM[name]
        try:
            return factory(cwd=cwd)
        except TypeError:
            return factory()
    if name not in _BUILTINS:
        raise UnknownAdapterError(f"Unknown adapter: {name}")
    module_name, class_name, accepts_cwd = _BUILTINS[name]
    try:
        module = importlib.import_module(module_name, package=__package__)
        cls = getattr(module, class_name)
    except Exception as exc:  # noqa: BLE001 - a broken integration must not break Core
        return MissingAdapter(name, f"{type(exc).__name__}: {exc}")
    # cwd is honored by the CLI-based adapters (worktree isolation,
    # parallel.py); localai_ollama is HTTP-based and has no cwd concept.
    return cls(cwd=cwd) if accepts_cwd else cls()


def project_repo_path(project_id: str | None) -> str | None:
    if not project_id:
        return None
    try:
        from ..registry import ProjectRegistry

        entry = ProjectRegistry().get(project_id)
    except Exception:  # noqa: BLE001 - no registry = no project cwd, not a crash
        return None
    return entry.repo_path if entry else None


def load_for_project(name: str, project_id: str | None, cwd: str | None = None) -> AgentAdapter:
    """The adapter as it should run for `project_id` (D66/D67):
    - cwd defaults to the project's registered repo_path. Before D66 the
      CLI-based adapters ran in whatever directory octavryn was started
      from, so claude_code/antigravity worked outside the project;
    - an adapter the project policy does not allow comes back as an
      unavailable MissingAdapter (routing skips it, execute() fails);
    - the policy's execution_profile for this adapter is applied.
    An invalid policy file makes every adapter unavailable (fail closed)."""
    from ..project_policy import PolicyError, policy_for

    try:
        pol = policy_for(project_id)
    except PolicyError as exc:
        return MissingAdapter(name, f"project policy invalid: {exc}")
    if pol is not None and not pol.adapter_allowed(name):
        return MissingAdapter(name, f"not allowed by the project policy for '{project_id}'")
    adapter = load_adapter(name, cwd=cwd if cwd is not None else project_repo_path(project_id))
    if pol is not None:
        profile = pol.execution_profile.get(name) or {}
        apply = getattr(adapter, "apply_profile", None)
        if profile and apply is None:
            return MissingAdapter(name, "project policy sets an execution_profile this adapter cannot enforce")
        if apply is not None:
            apply(profile)
    return adapter
