import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth
from solomon.dashboard import build_dashboard, render_dashboard
from solomon.models import Task, TaskStatus
from solomon.registry import ProjectEntry, ProjectRegistry
from solomon.result import TaskResult, Usage, UsageProvenance
from solomon.state import StateStore


class FakeRegistry(ProjectRegistry):
    def __init__(self, projects):
        self._projects = {p.project_id: p for p in projects}


def make_project(tmp_path, project_id="p1") -> ProjectEntry:
    notes_dir = tmp_path / project_id
    notes_dir.mkdir(exist_ok=True)
    (notes_dir / "note.md").write_text("---\ntitle: Note\n---\nhello world " * 20, encoding="utf-8")
    return ProjectEntry(project_id=project_id, name=f"Project {project_id}", knowledge_path=str(notes_dir))


def test_project_with_no_tasks_shows_none_progress_not_zero(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])
    data = build_dashboard(store, registry, {})
    assert data.projects[0].progress_pct is None
    assert data.projects[0].total_tasks == 0


def test_progress_reflects_task_status_counts(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])

    t1 = Task(goal_id="g", project_id="p1", type="t", role="coder", definition_of_done=["x"], status=TaskStatus.RESULT_RECEIVED)
    t2 = Task(goal_id="g", project_id="p1", type="t", role="coder", definition_of_done=["x"], status=TaskStatus.RUNNING)
    t3 = Task(goal_id="g", project_id="p1", type="t", role="coder", definition_of_done=["x"], status=TaskStatus.FAILED)
    for t in (t1, t2, t3):
        store.save_task(t)

    data = build_dashboard(store, registry, {})
    snap = data.projects[0]
    assert snap.total_tasks == 3
    assert snap.complete_tasks == 1
    assert snap.active_tasks == 1
    assert snap.failed_tasks == 1
    assert abs(snap.progress_pct - 33.33) < 0.5


def test_agent_health_and_stats_reflected(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])

    task = Task(goal_id="g", project_id="p1", type="t", role="coder", definition_of_done=["x"])
    store.save_task(task)
    store.save_result(
        TaskResult(
            task_id=task.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:03+00:00",
            usage=Usage(cost_usd=0.5, provenance=UsageProvenance.CLI_REPORTED),
        )
    )

    health_checks = {"claude_code": AdapterHealth(True, "ok"), "codex": AdapterHealth(False, "down")}
    data = build_dashboard(store, registry, health_checks)

    claude = next(a for a in data.agents if a.name == "claude_code")
    codex = next(a for a in data.agents if a.name == "codex")
    assert claude.available is True
    assert claude.stats["count"] == 1
    assert claude.stats["success_rate"] == 1.0
    assert codex.available is False
    assert data.projects[0].usage["total_cost_usd"] == 0.5


def test_pending_approvals_counted(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])
    store.create_approval_request("appr-1", "p1", "HIGH", "reason", {})

    data = build_dashboard(store, registry, {})
    assert data.projects[0].pending_approvals == 1
    assert data.pending_approvals_total == 1


def test_task_queue_only_lists_active_statuses(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])
    running = Task(goal_id="g", project_id="p1", type="t", role="coder", definition_of_done=["x"], status=TaskStatus.RUNNING)
    done = Task(goal_id="g", project_id="p1", type="t", role="coder", definition_of_done=["x"], status=TaskStatus.COMPLETE)
    store.save_task(running)
    store.save_task(done)

    data = build_dashboard(store, registry, {})
    ids = [t["task_id"] for t in data.task_queue]
    assert running.task_id in ids
    assert done.task_id not in ids


def test_knowledge_notes_counted_and_tokens_estimated(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])
    data = build_dashboard(store, registry, {})
    assert data.projects[0].knowledge_notes == 1
    assert data.projects[0].knowledge_raw_tokens_estimated > 0


def test_render_dashboard_does_not_raise(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path)])
    data = build_dashboard(store, registry, {"claude_code": AdapterHealth(True)})
    output = render_dashboard(data)
    assert "SOLOMON AI ORCHESTRATOR" in output
    assert "p1" in output


def test_project_id_filter_scopes_to_one_project(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    registry = FakeRegistry([make_project(tmp_path, "p1"), make_project(tmp_path, "p2")])
    data = build_dashboard(store, registry, {}, project_id="p1")
    assert len(data.projects) == 1
    assert data.projects[0].project.project_id == "p1"
