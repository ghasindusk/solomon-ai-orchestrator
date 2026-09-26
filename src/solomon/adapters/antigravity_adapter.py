"""antigravity adapter: wraps the `agy` CLI (Antigravity's non-interactive
print mode), not the `antigravity-ide` GUI launcher.

`antigravity-ide` (the desktop app binary) only exposes a `chat` subcommand
that opens a GUI window asynchronously with no way to capture a result --
not usable as a synchronous AgentAdapter. `agy` is a separate CLI
(installed at %LOCALAPPDATA%\\agy\\bin\\agy.exe) with a real non-interactive
`-p/--print --output-format json` mode returning a structured
{status, response, usage} payload, confirmed working in
08_Discovery/PHASE0_DISCOVERY_REPORT.md. That is what this adapter drives.

Capabilities per 04_Config_Schemas/agents.example.yaml: agentic_tasks,
environment_operations, multi_step_work, coding.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from ..models import Task
from ..result import TaskResult, Usage, UsageProvenance
from ..descriptors import Locality
from .base import AdapterDeclaration, AdapterHealth, AgentAdapter


class AntigravityAdapter(AgentAdapter):
    name = "antigravity"
    declaration = AdapterDeclaration(
        name="antigravity",
        display_name="Antigravity CLI (agy)",
        adapter_type="cli",
        provider="google",
        locality=Locality.CLOUD,
        capabilities=['agentic_tasks', 'environment_operations', 'multi_step_work', 'coding'],
        credentials="provider_managed",
        telemetry="none",
        mcp_tool_support=True,
    )

    def __init__(self, binary: str = "agy", cwd: str | None = None, sandbox: bool = True):
        self.binary = binary
        self.cwd = cwd
        self.sandbox = sandbox
        self.mode: str | None = None

    def apply_profile(self, profile: dict) -> None:
        """Project execution profile (D67). The sandbox can only be kept
        on (project_policy rejects sandbox: false); mode=plan keeps agy
        from editing files."""
        self.sandbox = True
        if profile.get("mode") in ("plan", "accept-edits"):
            self.mode = profile["mode"]

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
        cmd = [self.binary, "-p", prompt, "--output-format", "json"]
        if self.sandbox:
            cmd.append("--sandbox")
        if self.mode:
            cmd += ["--mode", self.mode]
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
                summary=f"antigravity execution timed out after {timeout_s}s",
                agent=self.name,
                started_at=started,
                uncertainties=["timeout"],
                raw_output=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"antigravity execution failed to start: {exc}",
                agent=self.name,
                started_at=started,
                uncertainties=["subprocess_error"],
            )

        parsed = None
        summary_text = proc.stdout.strip()
        usage = Usage(provenance=UsageProvenance.UNKNOWN)
        agy_status = None
        try:
            parsed = json.loads(proc.stdout)
            if isinstance(parsed, dict):
                summary_text = parsed.get("response") or summary_text
                agy_status = parsed.get("status")
                usage_info = parsed.get("usage")
                if isinstance(usage_info, dict):
                    usage = Usage(
                        input_tokens=usage_info.get("input_tokens"),
                        output_tokens=usage_info.get("output_tokens"),
                        provenance=UsageProvenance.CLI_REPORTED,
                    )
        except (json.JSONDecodeError, TypeError):
            pass

        status = (
            "RESULT_RECEIVED" if proc.returncode == 0 and agy_status in (None, "SUCCESS") else "FAILED"
        )
        return TaskResult(
            task_id=task.task_id,
            status=status,
            summary=summary_text[:2000] if summary_text else "(no output)",
            agent=self.name,
            started_at=started,
            usage=usage,
            raw_output=proc.stdout if not parsed else json.dumps(parsed, ensure_ascii=False)[:5000],
            returncode=proc.returncode,
            uncertainties=[] if status == "RESULT_RECEIVED" else [proc.stderr.strip()[:500] or f"agy status: {agy_status}"],
        )
