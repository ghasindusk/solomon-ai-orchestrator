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
from ..descriptors import Locality
from .base import AdapterDeclaration, NO_BACKGROUND_SUFFIX, AdapterHealth, AgentAdapter


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude_code"
    declaration = AdapterDeclaration(
        name="claude_code",
        display_name="Claude Code CLI",
        adapter_type="cli",
        provider="anthropic",
        locality=Locality.CLOUD,
        capabilities=['architecture', 'analysis', 'review', 'documentation', 'coding'],
        credentials="provider_managed",
        telemetry="tokens_and_cost",
        mcp_tool_support=True,
    )

    def __init__(self, binary: str = "claude", cwd: str | None = None):
        self.binary = binary
        self.cwd = cwd
        self.allowed_tools: list[str] | None = None
        self.disallowed_tools: list[str] = []
        self.permission_mode: str | None = None

    def apply_profile(self, profile: dict) -> None:
        """Project execution profile (D67). The user's own Claude Code
        settings may default to a permissive mode (e.g. `auto`); a profile
        with permission_mode=default plus an allowedTools list makes the
        non-interactive run deny every tool that is not listed."""
        if profile.get("allowed_tools") is not None:
            self.allowed_tools = list(profile["allowed_tools"])
        self.disallowed_tools = list(profile.get("disallowed_tools") or [])
        self.permission_mode = profile.get("permission_mode")

    def build_command(self, prompt: str) -> list[str]:
        cmd = [self.binary, "-p", prompt + NO_BACKGROUND_SUFFIX, "--output-format", "json"]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools is not None:
            cmd += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self.disallowed_tools)]
        return cmd

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
        cmd = self.build_command(prompt)
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
