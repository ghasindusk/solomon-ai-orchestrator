"""Adapter Layer interface (Architecture doc section 2).

Every provider adapter (localai_ollama, claude_code, codex, antigravity)
implements this interface. Router/scoring (Phase 3) is not implemented yet;
callers select an adapter explicitly by name for now.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from ..models import Task
from ..result import TaskResult


NO_BACKGROUND_SUFFIX = (
    "\n\nDo not start any background, detached, or run_in_background "
    "processes for this task. A non-interactive adapter invocation exits "
    "as soon as it reports its final result, which kills anything still "
    "running in the background and silently discards its work. Complete "
    "the task and report the final result synchronously within this "
    "single invocation."
)
"""Appended to every prompt sent to a CLI-based adapter that has its own
agentic tool-use loop (claude_code, codex). Added 2026-09-10 after a
claude_code invocation returned RESULT_RECEIVED with zero actual changes:
the raw response talked about "retrying" and "running in the background
now", strongly suggesting the child process backgrounded a long-running
step and then exited (as `-p` invocations do once they produce a result)
before that step finished. This is a blunt mitigation (a prompt suffix, not a
guarantee), not a fix for the underlying behavior, which is not yet
understood well enough to fix directly."""


class AdapterHealth:
    def __init__(self, available: bool, detail: str = ""):
        self.available = available
        self.detail = detail


class AgentAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def health(self) -> AdapterHealth:
        """Cheap check that the underlying CLI/API is reachable/usable."""

    @abstractmethod
    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        """Run the task non-interactively and return a structured TaskResult.

        Implementations must not raise on ordinary command failure; a
        non-zero exit or provider error is reported via TaskResult.status
        and TaskResult.uncertainties/evidence, so the Director can still
        record evidence and decide next steps (spec section 6: agent
        output is evidence, not completion).
        """

    def cancel(self, task: Task) -> None:  # pragma: no cover - optional
        """Best-effort cancellation hook; adapters without one may no-op."""
        return None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
