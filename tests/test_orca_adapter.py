import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.orca_adapter import OrcaAdapter
import solomon.adapters.registry as registry
from solomon.adapters.registry import known_adapter_names
from solomon.models import Task
from solomon.project_policy import PolicyError, ProjectPolicy


def make_task() -> Task:
    return Task(
        goal_id="g-orca",
        project_id="p-orca",
        type="implementation",
        role="coder",
        definition_of_done=["tests pass", "result recorded"],
        task_id="task-octavryn-1",
    )


def completed(args, payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def coordinator_terminal_command(cmd):
    if "terminal" in cmd and "create" in cmd:
        return completed(cmd, {"ok": True, "terminal": {"handle": "coord-1"}})
    if "terminal" in cmd and "close" in cmd:
        return completed(cmd, {"ok": True})
    return None


def success_sequence(outcome="succeeded"):
    def _run(cmd, **kwargs):
        terminal_result = coordinator_terminal_command(cmd)
        if terminal_result is not None:
            return terminal_result
        joined = " ".join(str(x) for x in cmd)
        if " status " in f" {joined} ":
            return completed(cmd, {"ok": True, "version": "test"})
        if "run-create" in cmd:
            return completed(cmd, {"ok": True, "run": {"runId": "run-1"}})
        if "worker-start" in cmd:
            return completed(
                cmd,
                {
                    "ok": True,
                    "task": {"taskId": "orca-task-1"},
                    "dispatch": {"dispatchId": "dispatch-1"},
                },
            )
        if "worker-release" in cmd:
            return completed(cmd, {"ok": True})
        if "--ack" in cmd:
            return completed(cmd, {"ok": True, "count": 0})
        if "check" in cmd and "--wait" in cmd:
            return completed(
                cmd,
                {
                    "ok": True,
                    "deliveryId": "delivery-1",
                    "messages": [
                        {
                            "type": "worker_done",
                            "subject": "worker settled",
                            "body": "implemented and tested",
                            "payload": {
                                "taskId": "orca-task-1",
                                "dispatchId": "dispatch-1",
                                "outcome": outcome,
                                "filesModified": ["src/example.py"],
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


def test_orca_is_execution_backend_not_routing_identity():
    assert "orca" not in known_adapter_names()


def test_project_can_execute_selected_codex_through_orca(monkeypatch):
    class Policy:
        execution_profile = {
            "codex": {
                "execution_backend": "orca",
                "orca": {"worktree": "current"},
            }
        }

        @staticmethod
        def adapter_allowed(name):
            return True

    monkeypatch.setattr("solomon.project_policy.policy_for", lambda project_id: Policy())
    monkeypatch.setattr(registry, "project_repo_path", lambda project_id: "/repo")

    adapter = registry.load_for_project("codex", "p-orca")
    assert isinstance(adapter, OrcaAdapter)
    assert adapter.agent == "codex"
    assert adapter.logical_agent == "codex"
    assert adapter.cwd == "/repo"


def test_project_can_execute_selected_claude_through_orca(monkeypatch):
    class Policy:
        execution_profile = {
            "claude_code": {
                "execution_backend": "orca",
                "orca": {"agent": "claude", "worktree": "current"},
            }
        }

        @staticmethod
        def adapter_allowed(name):
            return True

    monkeypatch.setattr("solomon.project_policy.policy_for", lambda project_id: Policy())
    monkeypatch.setattr(registry, "project_repo_path", lambda project_id: None)

    adapter = registry.load_for_project("claude_code", "p-orca")
    assert isinstance(adapter, OrcaAdapter)
    assert adapter.agent == "claude"
    assert adapter.logical_agent == "claude_code"


def test_orca_health_missing_binary():
    adapter = OrcaAdapter()
    with patch("solomon.adapters.orca_adapter.shutil.which", return_value=None):
        health = adapter.health()
    assert not health.available
    assert "not found" in health.detail


def test_orca_success_requires_matching_worker_done_and_releases_before_ack():
    adapter = OrcaAdapter(cwd="/repo", agent="codex")
    calls = []

    runner = success_sequence("succeeded")

    def _recording_run(cmd, **kwargs):
        calls.append(cmd)
        return runner(cmd, **kwargs)

    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run", side_effect=_recording_run
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "implemented and tested"
    assert result.changed_files == ["src/example.py"]
    assert "orca_run_id=run-1" in result.evidence
    assert "orca_coordinator_handle=coord-1" in result.evidence
    assert "orca_task_id=orca-task-1" in result.evidence
    assert "orca_dispatch_id=dispatch-1" in result.evidence

    worker_start = next(c for c in calls if "worker-start" in c)
    wait_check = next(c for c in calls if "check" in c and "--wait" in c)
    assert worker_start[worker_start.index("--run") + 1] == "run-1"
    assert worker_start[worker_start.index("--from") + 1] == "coord-1"
    assert wait_check[wait_check.index("--run") + 1] == "run-1"
    assert wait_check[wait_check.index("--terminal") + 1] == "coord-1"

    release_i = next(i for i, c in enumerate(calls) if "worker-release" in c)
    ack_i = next(i for i, c in enumerate(calls) if "--ack" in c)
    assert release_i < ack_i


def test_orca_worker_reported_failure_is_not_success():
    adapter = OrcaAdapter()
    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run",
        side_effect=success_sequence("failed"),
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "FAILED"
    assert "orca_outcome=failed" in result.evidence


def test_orca_mismatched_worker_done_fails_closed_without_ack_or_release():
    adapter = OrcaAdapter()
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        terminal_result = coordinator_terminal_command(cmd)
        if terminal_result is not None:
            return terminal_result
        joined = " ".join(str(x) for x in cmd)
        if " status " in f" {joined} ":
            return completed(cmd, {"ok": True})
        if "run-create" in cmd:
            return completed(cmd, {"ok": True, "run": {"runId": "run-1"}})
        if "worker-start" in cmd:
            return completed(
                cmd,
                {
                    "ok": True,
                    "taskId": "orca-task-1",
                    "dispatchId": "dispatch-1",
                },
            )
        if "check" in cmd and "--wait" in cmd:
            return completed(
                cmd,
                {
                    "ok": True,
                    "deliveryId": "delivery-bad",
                    "messages": [
                        {
                            "type": "worker_done",
                            "payload": {
                                "taskId": "other-task",
                                "dispatchId": "dispatch-1",
                                "outcome": "succeeded",
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run", side_effect=_run
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "FAILED"
    assert any("mismatched" in x for x in result.uncertainties)
    assert not any("worker-release" in c for c in calls)
    assert not any("--ack" in c for c in calls)


def test_orca_question_is_left_for_explicit_coordinator_action():
    adapter = OrcaAdapter()
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        terminal_result = coordinator_terminal_command(cmd)
        if terminal_result is not None:
            return terminal_result
        joined = " ".join(str(x) for x in cmd)
        if " status " in f" {joined} ":
            return completed(cmd, {"ok": True})
        if "run-create" in cmd:
            return completed(cmd, {"ok": True, "runId": "run-1"})
        if "worker-start" in cmd:
            return completed(cmd, {"ok": True, "taskId": "orca-task-1", "dispatchId": "dispatch-1"})
        if "check" in cmd and "--wait" in cmd:
            return completed(
                cmd,
                {
                    "ok": True,
                    "deliveryId": "delivery-q",
                    "messages": [
                        {
                            "type": "question",
                            "subject": "Need decision",
                            "payload": {
                                "taskId": "orca-task-1",
                                "dispatchId": "dispatch-1",
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run", side_effect=_run
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "FAILED"
    assert "requires coordinator action" in result.summary
    assert not any("--ack" in c for c in calls)


def test_orca_worker_start_failure_never_retries():
    adapter = OrcaAdapter()
    worker_start_calls = 0

    def _run(cmd, **kwargs):
        nonlocal worker_start_calls
        terminal_result = coordinator_terminal_command(cmd)
        if terminal_result is not None:
            return terminal_result
        joined = " ".join(str(x) for x in cmd)
        if " status " in f" {joined} ":
            return completed(cmd, {"ok": True})
        if "run-create" in cmd:
            return completed(cmd, {"ok": True, "runId": "run-1"})
        if "worker-start" in cmd:
            worker_start_calls += 1
            return completed(
                cmd,
                {
                    "ok": False,
                    "failedStage": "input_accepted",
                    "residualResources": ["terminal-1"],
                },
                returncode=1,
                stderr="worker start failed",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run", side_effect=_run
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "FAILED"
    assert worker_start_calls == 1
    assert any("no automatic retry" in x for x in result.uncertainties)


def test_orca_missing_run_id_fails_closed_before_worker_start():
    adapter = OrcaAdapter()
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        terminal_result = coordinator_terminal_command(cmd)
        if terminal_result is not None:
            return terminal_result
        joined = " ".join(str(x) for x in cmd)
        if " status " in f" {joined} ":
            return completed(cmd, {"ok": True})
        if "run-create" in cmd:
            return completed(cmd, {"ok": True, "result": {}})
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run", side_effect=_run
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "FAILED"
    assert any("missing Orca Run ID" in x for x in [result.summary])
    assert not any("worker-start" in call for call in calls)


def test_orca_release_failure_does_not_ack_delivery():
    adapter = OrcaAdapter()
    calls = []
    base = success_sequence("succeeded")

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if "worker-release" in cmd:
            return completed(cmd, {"ok": False, "error": {"code": "release_pending"}}, returncode=1)
        return base(cmd, **kwargs)

    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run", side_effect=_run
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "FAILED"
    assert any("cleanup_unverified" in item for item in result.uncertainties)
    assert not any("--ack" in call for call in calls)
    assert not any("terminal" in call and "close" in call for call in calls)


def test_orca_profile_accepts_agent_but_rejects_unimplemented_placement():
    adapter = OrcaAdapter()
    adapter.apply_profile({"agent": "claude", "worktree": "current"})
    assert adapter.agent == "claude"

    try:
        adapter.apply_profile({"worktree": "new-child"})
    except ValueError as exc:
        assert "current" in str(exc)
    else:
        raise AssertionError("non-current placement should fail closed in the pilot")


def test_invalid_orca_project_profile_fails_closed(monkeypatch):
    class Policy:
        execution_profile = {
            "codex": {
                "execution_backend": "orca",
                "orca": {"worktree": "new-child"},
            }
        }

        @staticmethod
        def adapter_allowed(name):
            return True

    monkeypatch.setattr("solomon.project_policy.policy_for", lambda project_id: Policy())
    monkeypatch.setattr(registry, "project_repo_path", lambda project_id: None)

    adapter = registry.load_for_project("codex", "p-orca")
    health = adapter.health()
    assert not health.available
    assert "invalid Orca execution_profile" in health.detail


def test_orca_backend_rejects_direct_only_provider_settings(monkeypatch):
    class Policy:
        execution_profile = {
            "codex": {
                "execution_backend": "orca",
                "sandbox": "read-only",
                "orca": {"worktree": "current"},
            }
        }

        @staticmethod
        def adapter_allowed(name):
            return True

    monkeypatch.setattr("solomon.project_policy.policy_for", lambda project_id: Policy())
    monkeypatch.setattr(registry, "project_repo_path", lambda project_id: None)

    adapter = registry.load_for_project("codex", "p-orca")
    health = adapter.health()
    assert not health.available
    assert "cannot be guaranteed through Orca" in health.detail


def test_orca_result_preserves_logical_provider_identity():
    adapter = OrcaAdapter(logical_agent="codex")
    with patch("solomon.adapters.orca_adapter.shutil.which", return_value="/usr/bin/orca"), patch(
        "solomon.adapters.orca_adapter.subprocess.run",
        side_effect=success_sequence("succeeded"),
    ):
        result = adapter.execute(make_task(), "implement feature", timeout_s=60)

    assert result.status == "RESULT_RECEIVED"
    assert result.agent == "codex"


def test_project_policy_accepts_provider_preserving_orca_backend():
    pol = ProjectPolicy.from_dict(
        "p-orca",
        {
            "allowed_adapters": ["codex"],
            "execution_profile": {
                "codex": {
                    "execution_backend": "orca",
                    "orca": {"agent": "codex", "worktree": "current"},
                }
            },
        },
    )
    assert pol.execution_profile["codex"]["execution_backend"] == "orca"


def test_project_policy_rejects_orca_with_direct_only_settings():
    try:
        ProjectPolicy.from_dict(
            "p-orca",
            {
                "execution_profile": {
                    "codex": {
                        "execution_backend": "orca",
                        "sandbox": "read-only",
                        "orca": {"worktree": "current"},
                    }
                }
            },
        )
    except PolicyError as exc:
        assert "cannot combine Orca" in str(exc)
    else:
        raise AssertionError("Orca backend must not pretend to enforce direct Codex sandbox settings")


def test_project_policy_rejects_orca_agent_identity_drift():
    try:
        ProjectPolicy.from_dict(
            "p-orca",
            {
                "execution_profile": {
                    "claude_code": {
                        "execution_backend": "orca",
                        "orca": {"agent": "codex", "worktree": "current"},
                    }
                }
            },
        )
    except PolicyError as exc:
        assert "Orca agent must remain" in str(exc)
    else:
        raise AssertionError("Orca backend must preserve the logical routing identity")
