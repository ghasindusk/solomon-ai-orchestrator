"""codex adapter: wraps the Codex CLI in non-interactive `exec` mode.

Capabilities per 04_Config_Schemas/agents.example.yaml: coding, debugging,
refactoring, testing, review.

The prompt is sent via stdin (`codex exec ... -`), never as a trailing
CLI argument. On Windows, codex resolves to an npm .CMD shim, which
subprocess.run(list) can't exec without shell=True (see _USE_SHELL
below); shell=True routes through cmd.exe, which truncates a
command-line argument at its first embedded newline -- multi-line
prompts (which review/debate prompts always are) would silently lose
everything after line 1. Confirmed this actually happened: a real
review-task run got "Line one." only and Codex asked for the input it
was never given. Passing the prompt as `input=` to subprocess.run
sidesteps cmd.exe's argument parsing entirely (stdin isn't part of the
command-line string), so shell=True stays safe to use for the fixed,
non-user-controlled flags that remain on the command line.

Uses `--json` (JSONL event stream on stdout) instead of `-o <tmpfile>`:
a `turn.completed` event carries real `usage.input_tokens`/
`output_tokens` (confirmed for real: codex exec DOES report usage, it
just isn't in the plain-text/`-o` output) and an `item.completed`
agent_message event carries the final response text, both far more
reliable to parse than scraping a plain-text file for "the last
message". This closes the "codex usage is always UNKNOWN" gap.

Sandbox workaround: if the resolved working directory sits under a path
listed in 04_Config_Schemas/codex_sandbox_workaround.yaml
(`incompatible_roots` -- currently just the Google Drive vault, see
sandbox_clone.py's docstring for the root cause), execute() transparently
runs codex against a local git clone on a real NTFS volume instead, and
syncs the result back with `git pull --ff-only` in both directions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from ..models import Task
from ..registry import ProjectRegistry
from ..result import TaskResult, Usage, UsageProvenance
from ..sandbox_clone import is_incompatible_path, local_clone_path, sync_after, sync_before
from .base import NO_BACKGROUND_SUFFIX, AdapterHealth, AgentAdapter

# codex is typically installed via npm, which shims it as a .CMD file on
# Windows; subprocess.run(list) uses CreateProcess directly and cannot exec
# .cmd files without going through the shell (WinError 2).
_USE_SHELL = sys.platform.startswith("win")


class CodexAdapter(AgentAdapter):
    name = "codex"

    def __init__(self, binary: str = "codex", cwd: str | None = None, sandbox: str = "workspace-write"):
        self.binary = binary
        self.cwd = cwd
        self.sandbox = sandbox

    def health(self) -> AdapterHealth:
        path = shutil.which(self.binary)
        if not path:
            return AdapterHealth(False, f"'{self.binary}' not found on PATH")
        try:
            proc = subprocess.run(
                [self.binary, "--version"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                shell=_USE_SHELL,
            )
            return AdapterHealth(proc.returncode == 0, proc.stdout.strip() or proc.stderr.strip())
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            return AdapterHealth(False, str(exc))

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        started = self._now()

        run_cwd = self.cwd
        active_clone: tuple[Path, Path] | None = None

        original_repo = self.cwd or self._project_repo_path(task.project_id)
        if original_repo and is_incompatible_path(original_repo):
            clone_path = local_clone_path(task.project_id) if task.project_id else None
            if clone_path is None:
                return TaskResult(
                    task_id=task.task_id,
                    status="FAILED",
                    summary=(
                        f"'{original_repo}' is on a Codex-sandbox-incompatible drive and no "
                        "local_clone_root/local_clone_paths entry is configured for this project in "
                        "04_Config_Schemas/codex_sandbox_workaround.yaml"
                    ),
                    agent=self.name,
                    started_at=started,
                    uncertainties=["subprocess_error"],
                )
            sync = sync_before(Path(original_repo), clone_path)
            if not sync.ok:
                return TaskResult(
                    task_id=task.task_id,
                    status="FAILED",
                    summary=f"codex sandbox workaround: {sync.detail}",
                    agent=self.name,
                    started_at=started,
                    uncertainties=["subprocess_error"],
                )
            run_cwd = str(clone_path)
            active_clone = (Path(original_repo), clone_path)

        cmd = [
            self.binary,
            "exec",
            "-s",
            self.sandbox,
            "--skip-git-repo-check",
            "--json",
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=prompt + NO_BACKGROUND_SUFFIX,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                cwd=run_cwd,
                shell=_USE_SHELL,
            )
        except subprocess.TimeoutExpired as exc:
            note = f" (any partial changes may be uncommitted in local clone {run_cwd})" if active_clone else ""
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"codex execution timed out after {timeout_s}s{note}",
                agent=self.name,
                started_at=started,
                uncertainties=["timeout"],
                raw_output=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"codex execution failed to start: {exc}",
                agent=self.name,
                started_at=started,
                uncertainties=["subprocess_error"],
            )

        summary_text, usage = self._parse_jsonl(proc.stdout)
        if not summary_text:
            summary_text = proc.stdout.strip()

        status = "RESULT_RECEIVED" if proc.returncode == 0 else "FAILED"
        uncertainties = [] if proc.returncode == 0 else [proc.stderr.strip()[:500]]

        if active_clone is not None:
            original_repo_path, clone_path = active_clone
            sync_back = sync_after(original_repo_path, clone_path, task.task_id)
            if sync_back.ok:
                summary_text = (summary_text or "") + f"\n[synced back from local sandbox clone {clone_path}]"
            else:
                # codex itself may have succeeded, but its changes never reached
                # original_repo (the source of truth) -- that's a task failure,
                # not a success, regardless of codex's own returncode.
                status = "FAILED"
                uncertainties.append("subprocess_error")
                summary_text = (summary_text or "") + (
                    f"\n[codex ran but sync-back to {original_repo_path} failed: {sync_back.detail}; "
                    f"changes remain committed in {clone_path}]"
                )

        return TaskResult(
            task_id=task.task_id,
            status=status,
            summary=summary_text[:2000] if summary_text else "(no output)",
            agent=self.name,
            started_at=started,
            usage=usage,
            raw_output=(proc.stdout + proc.stderr)[:5000],
            returncode=proc.returncode,
            uncertainties=uncertainties,
        )

    @staticmethod
    def _project_repo_path(project_id: str | None) -> str | None:
        if not project_id:
            return None
        try:
            project = ProjectRegistry().get(project_id)
        except Exception:  # noqa: BLE001 - registry lookup must not crash execute()
            return None
        return project.repo_path if project else None

    @staticmethod
    def _parse_jsonl(stdout: str) -> tuple[str, Usage]:
        """codex --json prints one JSON object per line, interleaved with
        the normal human-readable transcript on some builds -- skip any
        line that isn't valid JSON rather than failing the whole parse."""
        summary_text = ""
        usage = Usage(provenance=UsageProvenance.UNKNOWN)
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    summary_text = item.get("text") or summary_text
            elif event_type == "turn.completed":
                usage_info = event.get("usage") or {}
                if usage_info:
                    usage = Usage(
                        input_tokens=usage_info.get("input_tokens"),
                        output_tokens=usage_info.get("output_tokens"),
                        provenance=UsageProvenance.CLI_REPORTED,
                    )
        return summary_text, usage
