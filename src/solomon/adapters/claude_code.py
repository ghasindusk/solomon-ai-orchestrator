"""claude_code adapter: wraps the Claude Code CLI in non-interactive mode.

Capabilities per 04_Config_Schemas/agents.example.yaml: architecture,
analysis, review, documentation, coding.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from ..models import Task
from ..result import TaskResult, Usage, UsageProvenance
from .base import NO_BACKGROUND_SUFFIX, AdapterHealth, AgentAdapter


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude_code"

    def __init__(self, binary: str = "claude", cwd: str | None = None):
        self.binary = binary
        self.cwd = cwd

    def health(self) -> AdapterHealth:
        path = shutil.which(self.binary)
        if not path:
            return AdapterHealth(False, f"'{self.binary}' not found on PATH")
        try:
            proc = subprocess.run(
                [self.binary, "--version"], capture_output=True,
                encoding="utf-8", errors="replace", timeout=15
            )
            return AdapterHealth(proc.returncode == 0, proc.stdout.strip() or proc.stderr.strip())
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            return AdapterHealth(False, str(exc))

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        started = self._now()
        cmd = [
            self.binary,
            "-p",
            prompt + NO_BACKGROUND_SUFFIX,
            "--output-format",
            "json",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired as exc:
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"claude_code execution timed out after {timeout_s}s",
                agent=self.name,
                started_at=started,
                uncertainties=["timeout"],
                raw_output=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"claude_code execution failed to start: {exc}",
                agent=self.name,
                started_at=started,
                uncertainties=["subprocess_error"],
            )

        parsed = None
        summary_text = proc.stdout.strip()
        usage = Usage(provenance=UsageProvenance.UNKNOWN)
        try:
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                summary_text = parsed.get("result") or summary_text
                usage_info = parsed.get("usage")
                if isinstance(usage_info, dict):
                    usage = Usage(
                        input_tokens=usage_info.get("input_tokens"),
                        output_tokens=usage_info.get("output_tokens"),
                        cost_usd=parsed.get("total_cost_usd"),
                        provenance=UsageProvenance.CLI_REPORTED,
                    )
        except (json.JSONDecodeError, TypeError):
            pass

        status = "RESULT_RECEIVED" if proc.returncode == 0 else "FAILED"
        return TaskResult(
            task_id=task.task_id,
            status=status,
            summary=summary_text[:2000] if summary_text else "(no stdout)",
            agent=self.name,
            started_at=started,
            usage=usage,
            raw_output=proc.stdout if not parsed else json.dumps(parsed, ensure_ascii=False)[:5000],
            returncode=proc.returncode,
            uncertainties=[] if proc.returncode == 0 else [proc.stderr.strip()[:500]],
        )
