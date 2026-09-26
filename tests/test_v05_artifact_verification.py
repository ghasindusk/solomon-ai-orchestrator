"""D78: artifact-verified Definition of Done. A skill that declares the
`artifacts_in_prompt` verification is COMPLETE only when the artifact named
in the request exists in the repo, was written by this task, and passes the
project's verify command. The agent's own claim is not enough."""

import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.descriptors import SkillDescriptor, SkillScope
from solomon.models import Task
from solomon.registry import ProjectRegistry
from solomon.result import TaskResult
from solomon.skills import SkillRegistry
from solomon.state import StateStore
from solomon.verification import verify_definition_of_done

PATTERN = r"runs/[A-Za-z0-9_.-]+\.db"


def registry(tmp_path, verify_cmd):
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    body = (
        "version: 0.3\nprojects:\n  lab:\n    name: Lab\n"
        f"    repo_path: '{repo}'\n    vcs: git\n"
        f"    artifact_pattern: '{PATTERN}'\n"
    )
    if verify_cmd:
        body += f"    artifact_verify_command: '{verify_cmd}'\n"
    p = tmp_path / "reg.yaml"
    p.write_text(body, encoding="utf-8")
    return ProjectRegistry(p), repo


SKILL = SkillDescriptor(id="lab.backtest", version=1, name="b", required_capabilities=["backtesting"],
                        verification=["result_recorded", "artifacts_in_prompt"], scope=SkillScope.PROJECT)


def test_dod_expands_paths_from_the_prompt():
    dod = SkillRegistry.definition_of_done(SKILL, prompt="run it --audit runs/a.db then report runs/b.db",
                                           artifact_pattern=PATTERN)
    assert dod == ["result_recorded", "artifact:runs/a.db", "artifact:runs/b.db"]


def test_no_pattern_or_no_match_stays_unverifiable():
    assert SkillRegistry.definition_of_done(SKILL, prompt="runs/a.db", artifact_pattern=None)[-1] == \
        "skill_verification:artifacts_in_prompt"
    assert SkillRegistry.definition_of_done(SKILL, prompt="no file here", artifact_pattern=PATTERN)[-1] == \
        "skill_verification:artifacts_in_prompt"


def _task_with_result(store, started):
    t = Task(goal_id="g", project_id="lab", type="adhoc", role="researcher",
             definition_of_done=["result_recorded", "artifact:runs/a.db"])
    store.save_task(t)
    store.save_result(TaskResult(task_id=t.task_id, status="RESULT_RECEIVED", summary="done", agent="x",
                                 started_at=started.isoformat(), finished_at=started.isoformat()))
    return t


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "s.sqlite3")


def test_fresh_verified_artifact_satisfies(tmp_path, store):
    reg, repo = registry(tmp_path, f"{sys.executable} -c \"import sys; sys.exit(0)\" {{path}}")
    t = _task_with_result(store, datetime.now(timezone.utc) - timedelta(seconds=5))
    (repo / "runs" / "a.db").write_bytes(b"x")
    v = verify_definition_of_done(t, store, reg)
    assert "artifact:runs/a.db" in v.satisfied and not v.unsatisfied


def test_missing_artifact_is_unsatisfied(tmp_path, store):
    reg, repo = registry(tmp_path, None)
    t = _task_with_result(store, datetime.now(timezone.utc))
    assert "artifact:runs/a.db" in verify_definition_of_done(t, store, reg).unsatisfied


def test_preexisting_artifact_cannot_vouch_for_the_task(tmp_path, store):
    reg, repo = registry(tmp_path, None)
    f = repo / "runs" / "a.db"
    f.write_bytes(b"old")
    old = time.time() - 3600
    os.utime(f, (old, old))
    t = _task_with_result(store, datetime.now(timezone.utc))
    assert "artifact:runs/a.db" in verify_definition_of_done(t, store, reg).unsatisfied


def test_failing_verify_command_is_unsatisfied(tmp_path, store):
    reg, repo = registry(tmp_path, f"{sys.executable} -c \"import sys; sys.exit(1)\" {{path}}")
    t = _task_with_result(store, datetime.now(timezone.utc) - timedelta(seconds=5))
    (repo / "runs" / "a.db").write_bytes(b"x")
    assert "artifact:runs/a.db" in verify_definition_of_done(t, store, reg).unsatisfied


def test_path_outside_the_pattern_is_never_checked(tmp_path, store):
    reg, repo = registry(tmp_path, f"{sys.executable} -c \"import sys; sys.exit(0)\" {{path}}")
    t = Task(goal_id="g", project_id="lab", type="adhoc", role="researcher",
             definition_of_done=["artifact:../outside.db", "artifact:runs/a.db & calc.exe"])
    store.save_task(t)
    v = verify_definition_of_done(t, store, reg)
    assert set(v.unsatisfied) == {"artifact:../outside.db", "artifact:runs/a.db & calc.exe"}


def test_project_without_pattern_is_unverifiable(tmp_path, store):
    p = tmp_path / "reg.yaml"
    p.write_text("version: 0.3\nprojects:\n  lab: {name: Lab, repo_path: '.'}\n", encoding="utf-8")
    t = _task_with_result(store, datetime.now(timezone.utc))
    assert "artifact:runs/a.db" in verify_definition_of_done(t, store, ProjectRegistry(p)).unverifiable


def test_evaluation_outcome_distinguishes_failed_verification():
    from solomon.evaluation import outcome_for

    assert outcome_for("RESULT_RECEIVED", "COMPLETE") == "verified_success"
    assert outcome_for("RESULT_RECEIVED", "REMEDIATION_REQUIRED") == "verification_failed"
    assert outcome_for("RESULT_RECEIVED", "RESULT_RECEIVED") == "success"
    assert outcome_for("FAILED", "FAILED") == "failure" and outcome_for(None, "BLOCKED") == "unknown"
