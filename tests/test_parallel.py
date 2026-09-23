import subprocess
import sys
import pathlib
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.models import Task
from solomon.parallel import BatchItem, run_batch
from solomon.result import TaskResult
from solomon.router import Router
from solomon.state import StateStore
from solomon.worktree import WorktreeManager


class SleepyAdapter(AgentAdapter):
    def __init__(self, name, sleep_s=0.15):
        self.name = name
        self.sleep_s = sleep_s
        self._lock = threading.Lock()
        self.execute_calls = 0

    def health(self) -> AdapterHealth:
        return AdapterHealth(True)

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        with self._lock:
            self.execute_calls += 1
        time.sleep(self.sleep_s)
        return TaskResult(
            task_id=task.task_id, status="RESULT_RECEIVED", summary=prompt, agent=self.name,
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
        )


def make_task(project_id="p1") -> Task:
    return Task(goal_id="g1", project_id=project_id, type="t", role="coder", definition_of_done=["x"])


def _restrict(router, monkeypatch, names):
    monkeypatch.setattr(router, "candidates_for_role", lambda role: list(names))


class CwdRecordingAdapter(AgentAdapter):
    def __init__(self, name, cwd=None):
        self.name = name
        self.cwd = cwd
        self.execute_calls = 0

    def health(self) -> AdapterHealth:
        return AdapterHealth(True)

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        self.execute_calls += 1
        return TaskResult(
            task_id=task.task_id, status="RESULT_RECEIVED", summary=f"ran in {self.cwd}", agent=self.name,
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:01+00:00",
        )


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=True
    )


def make_repo(tmp_path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial commit", cwd=repo)
    return repo


def test_independent_tasks_run_concurrently(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    adapter = SleepyAdapter("claude_code", sleep_s=0.15)

    items = [BatchItem(task=make_task(), prompt=f"job {i}") for i in range(4)]
    started = time.monotonic()
    outcomes = run_batch(items, router, lambda name: adapter, store, max_workers=4)
    elapsed = time.monotonic() - started

    assert all(o.status == "completed" for o in outcomes)
    # 4 x 0.15s serial would be >= 0.6s; concurrent should land well under that
    assert elapsed < 0.5
    assert adapter.execute_calls == 4


def test_lock_conflict_defers_one_task(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    adapter = SleepyAdapter("claude_code", sleep_s=0.2)

    same_path = str(tmp_path / "shared_file.py")
    items = [
        BatchItem(task=make_task(), prompt="job A", touches=[same_path]),
        BatchItem(task=make_task(), prompt="job B", touches=[same_path]),
    ]
    outcomes = run_batch(items, router, lambda name: adapter, store, max_workers=2)

    statuses = sorted(o.status for o in outcomes)
    assert statuses == ["completed", "deferred_lock_conflict"]
    # deferred task must not have been executed
    assert adapter.execute_calls == 1


def test_no_touches_tasks_never_lock(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    adapter = SleepyAdapter("claude_code", sleep_s=0.05)

    items = [BatchItem(task=make_task(), prompt=f"job {i}") for i in range(3)]
    outcomes = run_batch(items, router, lambda name: adapter, store, max_workers=3)
    assert all(o.status == "completed" for o in outcomes)


def test_unknown_role_reports_no_candidate(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, [])
    adapter = SleepyAdapter("claude_code", sleep_s=0.01)

    items = [BatchItem(task=make_task(), prompt="job")]
    outcomes = run_batch(items, router, lambda name: adapter, store, max_workers=1)
    assert outcomes[0].status == "no_candidate"


def test_task_status_updated_after_completion(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    adapter = SleepyAdapter("claude_code", sleep_s=0.01)

    item = BatchItem(task=make_task(), prompt="job")
    run_batch([item], router, lambda name: adapter, store, max_workers=1)
    saved = store.list_tasks(project_id=item.task.project_id)
    assert saved[0]["task_id"] == item.task.task_id
    assert saved[0]["status"] == "RESULT_RECEIVED"


def test_results_are_saved_to_state(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    adapter = SleepyAdapter("claude_code", sleep_s=0.01)

    item = BatchItem(task=make_task(), prompt="job")
    run_batch([item], router, lambda name: adapter, store, max_workers=1)
    saved = store.get_task_results(item.task.task_id)
    assert len(saved) == 1
    assert saved[0]["status"] == "RESULT_RECEIVED"


# --- worktree isolation (real git repos, not mocked) ---

def test_worktree_task_executes_with_isolated_cwd(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)

    item = BatchItem(task=make_task(), prompt="job", use_worktree=True)
    outcomes = run_batch(
        [item], router, lambda name, cwd=None: CwdRecordingAdapter(name, cwd=cwd), store,
        max_workers=1, worktree_manager=manager, worktrees_root=tmp_path / "worktrees",
    )
    assert outcomes[0].status == "completed"
    assert outcomes[0].worktree_path is not None
    assert (tmp_path / "worktrees" / item.task.task_id) == pathlib.Path(outcomes[0].worktree_path)


def test_worktree_record_saved_to_state(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)

    item = BatchItem(task=make_task(), prompt="job", use_worktree=True)
    run_batch(
        [item], router, lambda name, cwd=None: CwdRecordingAdapter(name, cwd=cwd), store,
        max_workers=1, worktree_manager=manager, worktrees_root=tmp_path / "worktrees",
    )
    wt = store.get_worktree(item.task.task_id)
    assert wt is not None
    assert wt["status"] == "active"
    assert wt["branch"] == f"solomon/{item.task.task_id}"


def test_two_worktree_tasks_touching_same_path_both_succeed(tmp_path, monkeypatch):
    # The whole point of worktree isolation: unlike `touches` path locks,
    # two tasks editing the "same" logical path never conflict because
    # each has its own isolated working directory.
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    repo = make_repo(tmp_path)
    manager = WorktreeManager(repo)

    items = [
        BatchItem(task=make_task(), prompt="job A", use_worktree=True),
        BatchItem(task=make_task(), prompt="job B", use_worktree=True),
    ]
    outcomes = run_batch(
        items, router, lambda name, cwd=None: CwdRecordingAdapter(name, cwd=cwd), store,
        max_workers=2, worktree_manager=manager, worktrees_root=tmp_path / "worktrees",
    )
    assert all(o.status == "completed" for o in outcomes)
    assert outcomes[0].worktree_path != outcomes[1].worktree_path


def test_worktree_requested_without_manager_fails_gracefully(tmp_path, monkeypatch):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store)
    _restrict(router, monkeypatch, ["claude_code"])
    adapter = SleepyAdapter("claude_code", sleep_s=0.01)

    item = BatchItem(task=make_task(), prompt="job", use_worktree=True)
    outcomes = run_batch([item], router, lambda name: adapter, store, max_workers=1)
    assert outcomes[0].status == "worktree_failed"
    assert adapter.execute_calls == 0
