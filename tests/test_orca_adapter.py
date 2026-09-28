import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.orca_adapter import OrcaAdapter
from solomon.adapters.registry import known_adapter_names, load_adapter
from solomon.models import Task


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


def success_sequence(outcome="succeeded"):
    def _run(cmd, **kwargs):
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


def test_registry_exposes_orca():
    assert "orca" in known_adapter_names()
    assert isinstance(load_adapter("orca"), OrcaAdapter)


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
    assert "orca_task_id=orca-task-1" in result.evidence
    assert "orca_dispatch_id=dispatch-1" in result.evidence

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
    import solomon.adapters.registry as registry

    class Policy:
        execution_profile = {"orca": {"worktree": "new-child"}}

        @staticmethod
        def adapter_allowed(name):
            return True

    monkeypatch.setattr("solomon.project_policy.policy_for", lambda project_id: Policy())
    monkeypatch.setattr(registry, "project_repo_path", lambda project_id: None)

    adapter = registry.load_for_project("orca", "p-orca")
    health = adapter.health()
    assert not health.available
    assert "invalid execution_profile" in health.detail
