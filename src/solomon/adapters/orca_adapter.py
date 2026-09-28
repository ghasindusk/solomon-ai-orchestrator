"""Orca execution-plane adapter for Octavryn SI.

This adapter deliberately treats Orca as a supervised execution substrate,
not as a second policy/router layer. Octavryn still owns routing, governance,
context, Definition-of-Done verification, and final task state. Orca owns the
worker lifecycle, terminal/worktree execution, and durable worker_done
provenance.

The first integration slice is intentionally conservative:
- it uses Orca's documented supervised orchestration loop;
- it runs in the current Orca-managed worktree only;
- a TaskResult is RESULT_RECEIVED only after a worker_done whose taskId and
  dispatchId match the authoritative worker-start receipt;
- questions/escalations and unverified completion never become success;
- no automatic retry is attempted after an uncertain dispatch.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..descriptors import Locality
from ..models import Task
from ..result import TaskResult, Usage, UsageProvenance
from .base import AdapterDeclaration, AdapterHealth, AgentAdapter


_SAFE_AGENT = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str
    payload: object | None


class OrcaAdapter(AgentAdapter):
    name = "orca"
    declaration = AdapterDeclaration(
        name="orca",
        display_name="Orca Execution Plane",
        adapter_type="cli",
        provider="stablyai/orca",
        locality=Locality.HYBRID,
        capabilities=[
            "coding",
            "debugging",
            "refactoring",
            "testing",
            "review",
            "parallel_execution",
            "worktree_isolation",
            "supervised_orchestration",
        ],
        credentials="provider_managed",
        telemetry="none",
    )

    def __init__(
        self,
        binary: str = "orca",
        cwd: str | None = None,
        agent: str = "codex",
        worktree: str = "current",
        logical_agent: str | None = None,
    ):
        self.binary = binary
        self.cwd = cwd
        self.agent = agent
        self.logical_agent = logical_agent or agent
        self.worktree = worktree

    def apply_profile(self, profile: dict) -> None:
        """Apply the small, explicitly supported v0.6 Orca profile surface.

        Agent names are passed as argv, never through a shell. The first
        integration slice intentionally permits only Orca's documented
        current-worktree placement; automatic repo registration/new worktree
        placement belongs to the next integration slice.
        """
        agent = profile.get("agent")
        if agent is not None:
            if not isinstance(agent, str) or not _SAFE_AGENT.fullmatch(agent):
                raise ValueError("orca execution_profile.agent must match [A-Za-z0-9_.-]+")
            self.agent = agent

        worktree = profile.get("worktree")
        if worktree is not None:
            if worktree != "current":
                raise ValueError("orca v0.6 pilot supports execution_profile.worktree='current' only")
            self.worktree = worktree

    def health(self) -> AdapterHealth:
        executable = shutil.which(self.binary)
        if not executable:
            return AdapterHealth(False, f"'{self.binary}' not found on PATH")
        if executable.lower().endswith((".cmd", ".bat")):
            return AdapterHealth(
                False,
                "Orca resolved to a cmd/bat shim; configure the native Orca executable "
                "so Octavryn can invoke it without a command shell",
            )

        result = self._invoke(["status"], timeout_s=20)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            return AdapterHealth(False, f"orca status failed: {detail[:400]}")

        if isinstance(result.payload, dict) and result.payload.get("ok") is False:
            return AdapterHealth(False, f"Orca runtime not ready: {self._payload_summary(result.payload)[:350]}")

        version = self._deep_value(result.payload, {"version"})
        suffix = f" ({version})" if version else ""
        return AdapterHealth(True, f"Orca runtime ready{suffix}")

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        started = self._now()
        deadline = time.monotonic() + max(1, timeout_s)

        health = self.health()
        if not health.available:
            return self._failed(
                task,
                started,
                f"Orca is unavailable: {health.detail}",
                ["unavailable: orca runtime"],
            )

        # Octavryn is an external coordinator process, not an interactive
        # command typed inside an Orca terminal. Give it an explicit Orca
        # coordinator identity so Run binding and inbox consumption never
        # depend on whichever terminal happens to be active in the UI.
        coordinator = self._invoke(
            [
                "terminal",
                "create",
                "--worktree",
                "active",
                "--title",
                f"Octavryn coordinator {task.task_id}",
            ],
            timeout_s=self._remaining(deadline, cap=30),
        )
        if coordinator.returncode != 0 or self._payload_failed(coordinator.payload):
            return self._command_failure(task, started, "coordinator terminal create", coordinator)

        coordinator_handle = self._deep_value(
            coordinator.payload, {"handle", "terminalHandle", "terminal_handle"}
        )
        if not coordinator_handle:
            return self._failed(
                task,
                started,
                "Orca terminal create succeeded without an authoritative coordinator handle; "
                "Octavryn will not create an unscoped orchestration Run.",
                ["unverifiable: missing Orca coordinator terminal handle"],
                raw=coordinator.stdout + coordinator.stderr,
            )
        coordinator_handle = str(coordinator_handle)

        spec = self._task_spec(task, prompt)

        run = self._invoke(
            [
                "orchestration",
                "run-create",
                "--objective",
                f"Octavryn {task.task_id}: {task.type}",
                "--from",
                coordinator_handle,
            ],
            timeout_s=self._remaining(deadline, cap=30),
        )
        if run.returncode != 0 or self._payload_failed(run.payload):
            # Once run-create was attempted, mutation may be ambiguous.
            # Retain the coordinator terminal for inspection rather than
            # closing it and potentially orphaning a Run that actually exists.
            return self._command_failure(
                task,
                started,
                "run-create",
                run,
                extra_uncertainty="unverifiable: coordinator terminal retained after Run creation failure",
                evidence=[f"orca_coordinator_handle={coordinator_handle}"],
            )

        run_id = self._prefixed_id(run.payload, "run_") or self._deep_value(
            run.payload, {"runId", "run_id"}
        )
        if not run_id:
            return self._failed(
                task,
                started,
                "Orca run-create did not return an authoritative Run ID; Octavryn will not "
                "start a worker against an implicitly scoped Run.",
                ["unverifiable: missing Orca Run ID"],
                evidence=[f"orca_coordinator_handle={coordinator_handle}"],
                raw=run.stdout + run.stderr,
            )
        run_id = str(run_id)

        worker = self._invoke(
            [
                "orchestration",
                "worker-start",
                "--spec",
                spec,
                "--worktree",
                self.worktree,
                "--agent",
                self.agent,
                "--run",
                run_id,
                "--from",
                coordinator_handle,
            ],
            timeout_s=self._remaining(deadline, cap=90),
        )
        if worker.returncode != 0 or self._payload_failed(worker.payload):
            # Orca's contract says not to relaunch a failed worker-start:
            # its receipt may contain residualResources/failedStage.
            return self._command_failure(
                task,
                started,
                "worker-start",
                worker,
                extra_uncertainty="unverifiable: worker-start failed; no automatic retry was attempted",
                evidence=[
                    f"orca_run_id={run_id}",
                    f"orca_coordinator_handle={coordinator_handle}",
                ],
            )

        orca_task_id = self._deep_value(worker.payload, {"taskId", "task_id"})
        dispatch_id = self._deep_value(worker.payload, {"dispatchId", "dispatch_id"})
        if not orca_task_id or not dispatch_id:
            return self._failed(
                task,
                started,
                "Orca worker-start did not return both authoritative taskId and dispatchId; "
                "Octavryn will not infer lifecycle authority.",
                ["unverifiable: missing Orca lifecycle identifiers"],
                evidence=[
                    f"orca_run_id={run_id}",
                    f"orca_coordinator_handle={coordinator_handle}",
                ],
                raw=worker.stdout + worker.stderr,
            )

        base_evidence = [
            f"orca_run_id={run_id}",
            f"orca_coordinator_handle={coordinator_handle}",
            f"orca_task_id={orca_task_id}",
            f"orca_dispatch_id={dispatch_id}",
        ]
        raw_parts = [coordinator.stdout, run.stdout, worker.stdout]
        empty_waits = 0
        last_inspection: str | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                evidence = list(base_evidence)
                if last_inspection:
                    evidence.append(f"orca_worker_inspection={last_inspection[:1200]}")
                return self._failed(
                    task,
                    started,
                    "Octavryn's wait window ended without authoritative Orca worker_done. "
                    "The Orca worker and coordinator terminal were not stopped or retried and "
                    f"may still be live; inspect Run {run_id} / dispatch {dispatch_id} before retrying.",
                    ["unverifiable: Orca completion was not observed"],
                    evidence=evidence,
                    raw="\n".join(raw_parts),
                )

            wait_ms = max(1, min(int(remaining * 1000), 900_000))
            checked = self._invoke(
                [
                    "orchestration",
                    "check",
                    "--terminal",
                    coordinator_handle,
                    "--run",
                    run_id,
                    "--wait",
                    "--types",
                    "worker_done,escalation,question",
                    "--timeout-ms",
                    str(wait_ms),
                ],
                timeout_s=min(remaining + 15, (wait_ms / 1000) + 15),
            )
            raw_parts.append(checked.stdout)

            if checked.returncode != 0 or self._payload_failed(checked.payload):
                return self._command_failure(
                    task,
                    started,
                    "orchestration check",
                    checked,
                    extra_uncertainty=(
                        f"unverifiable: dispatch {dispatch_id} and coordinator terminal "
                        f"{coordinator_handle} were retained"
                    ),
                    evidence=list(base_evidence),
                    raw="\n".join(raw_parts),
                )

            messages = list(self._messages(checked.payload))
            if not messages:
                empty_waits += 1
                if empty_waits == 3:
                    # The current Orca contract recommends an explicit Run
                    # enumeration after repeated empty waits. It is inspection
                    # only: no timeout authorizes stop/retry/release.
                    inspection = self._invoke(
                        [
                            "orchestration",
                            "worker-list",
                            "--run",
                            run_id,
                            "--include-remote",
                        ],
                        timeout_s=self._remaining(deadline, cap=30),
                    )
                    last_inspection = inspection.stdout.strip() or inspection.stderr.strip()
                    raw_parts.append(last_inspection)
                continue

            empty_waits = 0
            matching_done: dict[str, Any] | None = None
            blocker: dict[str, Any] | None = None

            for message in messages:
                msg_type = str(message.get("type") or message.get("messageType") or "").lower()
                if msg_type == "worker_done":
                    msg_task = self._deep_value(message, {"taskId", "task_id"})
                    msg_dispatch = self._deep_value(message, {"dispatchId", "dispatch_id"})
                    if str(msg_task) == str(orca_task_id) and str(msg_dispatch) == str(dispatch_id):
                        matching_done = message
                    else:
                        return self._failed(
                            task,
                            started,
                            "Orca delivered worker_done with lifecycle IDs that do not match "
                            "the authoritative worker-start receipt; delivery was left unacknowledged.",
                            ["unverifiable: mismatched Orca lifecycle identifiers"],
                            evidence=list(base_evidence),
                            raw="\n".join(raw_parts),
                        )
                elif msg_type in {"question", "escalation"}:
                    blocker = message

            if blocker is not None:
                msg_type = str(blocker.get("type") or blocker.get("messageType") or "blocker")
                subject = self._deep_value(blocker, {"subject", "body"}) or "(no details)"
                return self._failed(
                    task,
                    started,
                    f"Orca worker requires coordinator action ({msg_type}): {str(subject)[:900]}. "
                    "The delivery and coordinator terminal were retained so the action can be "
                    "handled explicitly.",
                    [f"orca_{msg_type}_requires_action"],
                    evidence=list(base_evidence),
                    raw="\n".join(raw_parts),
                )

            if matching_done is None:
                # A delivery can contain unrelated informational messages in
                # future Orca versions. Do not acknowledge what we did not
                # understand, and do not infer completion.
                return self._failed(
                    task,
                    started,
                    "Orca returned a delivery without a matching worker_done; "
                    "Octavryn left it unacknowledged rather than guessing.",
                    ["unverifiable: unrecognized Orca delivery"],
                    evidence=list(base_evidence),
                    raw="\n".join(raw_parts),
                )

            outcome = str(self._deep_value(matching_done, {"outcome"}) or "").lower()
            body = self._deep_value(matching_done, {"body"})
            subject = self._deep_value(matching_done, {"subject"})
            summary = str(body or subject or f"Orca worker_done for {orca_task_id}")
            files = self._deep_value(matching_done, {"filesModified", "files_modified"})
            changed_files = [str(x) for x in files] if isinstance(files, list) else []

            # Settlement authorizes cleanup, but cleanup must itself be proven
            # before the Delivery is acknowledged. Otherwise the evidence that
            # tells a coordinator what still needs release could be consumed.
            release = self._invoke(
                ["orchestration", "worker-release", "--dispatch", str(dispatch_id)],
                timeout_s=self._remaining(deadline, cap=30),
            )
            raw_parts.append(release.stdout)
            if release.returncode != 0 or self._payload_failed(release.payload):
                return self._failed(
                    task,
                    started,
                    "Orca worker_done was authoritative, but worker-release did not confirm "
                    "cleanup. The Delivery was intentionally left unacknowledged and the "
                    "coordinator terminal was retained for recovery.",
                    ["orca_cleanup_unverified: worker-release did not confirm cleanup"],
                    evidence=[*base_evidence, f"orca_outcome={outcome or 'unknown'}"],
                    raw="\n".join(raw_parts),
                )

            cleanup_uncertainties: list[str] = []
            delivery_id = self._deep_value(checked.payload, {"deliveryId", "delivery_id"})
            if delivery_id:
                ack = self._invoke(
                    [
                        "orchestration",
                        "check",
                        "--terminal",
                        coordinator_handle,
                        "--run",
                        run_id,
                        "--ack",
                        str(delivery_id),
                    ],
                    timeout_s=self._remaining(deadline, cap=30),
                )
                raw_parts.append(ack.stdout)
                if ack.returncode != 0 or self._payload_failed(ack.payload):
                    cleanup_uncertainties.append("orca_ack_failed: delivery may replay")
            else:
                cleanup_uncertainties.append("orca_ack_skipped: delivery id missing")

            # Only close Octavryn's coordinator terminal after a confirmed ACK.
            # If ACK failed or no delivery id was returned, retaining the
            # terminal preserves an identity from which replay can be repaired.
            if not cleanup_uncertainties:
                close = self._invoke(
                    ["terminal", "close", "--terminal", coordinator_handle],
                    timeout_s=self._remaining(deadline, cap=30),
                )
                raw_parts.append(close.stdout)
                if close.returncode != 0 or self._payload_failed(close.payload):
                    cleanup_uncertainties.append(
                        "orca_coordinator_cleanup_failed: coordinator terminal may remain"
                    )

            status = "RESULT_RECEIVED" if outcome == "succeeded" else "FAILED"
            if outcome not in {"succeeded", "failed"}:
                status = "FAILED"
                cleanup_uncertainties.insert(0, "unverifiable: worker_done outcome missing or unknown")

            return TaskResult(
                task_id=task.task_id,
                status=status,
                summary=summary[:2000],
                agent=self.logical_agent,
                started_at=started,
                changed_files=changed_files,
                evidence=[
                    *base_evidence,
                    f"orca_outcome={outcome or 'unknown'}",
                ],
                uncertainties=cleanup_uncertainties,
                usage=Usage(provenance=UsageProvenance.UNKNOWN),
                raw_output="\n".join(raw_parts)[:5000],
                returncode=0 if status == "RESULT_RECEIVED" else 1,
            )

    def _invoke(self, args: list[str], timeout_s: float) -> _CommandResult:
        executable = shutil.which(self.binary) or self.binary
        cmd = [executable, *args, "--json"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, timeout_s),
                cwd=self.cwd,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _CommandResult(124, "", f"timeout: {exc}", None)
        except Exception as exc:  # noqa: BLE001 - adapters surface execution errors
            return _CommandResult(127, "", f"{type(exc).__name__}: {exc}", None)

        return _CommandResult(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            self._parse_json(proc.stdout),
        )

    @staticmethod
    def _parse_json(stdout: str) -> object | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    @classmethod
    def _walk_dicts(cls, value: object) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_dicts(child)

    @classmethod
    def _deep_value(cls, value: object, keys: set[str]) -> Any:
        for item in cls._walk_dicts(value):
            for key in keys:
                if key in item and item[key] is not None:
                    return item[key]
        return None

    @classmethod
    def _messages(cls, payload: object) -> Iterable[dict[str, Any]]:
        seen: set[int] = set()
        for item in cls._walk_dicts(payload):
            item_id = id(item)
            if item_id in seen:
                continue
            msg_type = str(item.get("type") or item.get("messageType") or "").lower()
            if msg_type in {"worker_done", "question", "escalation"}:
                seen.add(item_id)
                yield item

    @staticmethod
    def _payload_failed(payload: object | None) -> bool:
        return isinstance(payload, dict) and payload.get("ok") is False

    @classmethod
    def _payload_summary(cls, payload: object | None) -> str:
        if payload is None:
            return "(no JSON payload)"
        if isinstance(payload, (dict, list)):
            try:
                return json.dumps(payload, ensure_ascii=False)
            except TypeError:
                pass
        return str(payload)

    @staticmethod
    def _remaining(deadline: float, cap: float) -> float:
        return max(1.0, min(cap, deadline - time.monotonic()))

    @staticmethod
    def _task_spec(task: Task, prompt: str) -> str:
        dod = "; ".join(task.definition_of_done) if task.definition_of_done else "(not provided)"
        return (
            f"Target: Octavryn project {task.project_id}.\n"
            f"Change: {prompt}\n"
            "Constraints: Work only on this Octavryn task. Do not broaden scope. "
            "Report uncertainty instead of claiming unverified completion.\n"
            f"Ownership: this worker owns only task {task.task_id}.\n"
            f"Observable acceptance: {dod}"
        )

    def _command_failure(
        self,
        task: Task,
        started: str,
        stage: str,
        command: _CommandResult,
        extra_uncertainty: str | None = None,
        evidence: list[str] | None = None,
        raw: str | None = None,
    ) -> TaskResult:
        detail = command.stderr.strip() or command.stdout.strip() or self._payload_summary(command.payload)
        uncertainties = [f"orca_{stage.replace(' ', '_')}_failed"]
        if extra_uncertainty:
            uncertainties.append(extra_uncertainty)
        return self._failed(
            task,
            started,
            f"Orca {stage} failed: {detail[:1200]}",
            uncertainties,
            evidence=evidence,
            raw=raw if raw is not None else command.stdout + command.stderr,
            returncode=command.returncode,
        )

    def _failed(
        self,
        task: Task,
        started: str,
        summary: str,
        uncertainties: list[str],
        *,
        evidence: list[str] | None = None,
        raw: str | None = None,
        returncode: int | None = 1,
    ) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            status="FAILED",
            summary=summary[:2000],
            agent=self.logical_agent,
            started_at=started,
            uncertainties=uncertainties,
            evidence=evidence or [],
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            raw_output=raw[:5000] if raw else raw,
            returncode=returncode,
        )
