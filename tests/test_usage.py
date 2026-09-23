import sys
import pathlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.models import Task
from solomon.result import TaskResult, Usage, UsageProvenance
from solomon.state import StateStore
from solomon.usage import UsageManager, _window_since


def make_store(tmp_path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.sqlite3")


def seed_task_with_cost(store, project_id, agent, cost_usd, finished_at="2026-01-01T00:00:02+00:00"):
    task = Task(goal_id="g1", project_id=project_id, type="t", role="coder", definition_of_done=["x"])
    store.save_task(task)
    result = TaskResult(
        task_id=task.task_id,
        status="RESULT_RECEIVED",
        summary="ok",
        agent=agent,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at=finished_at,
        usage=Usage(input_tokens=100, output_tokens=50, cost_usd=cost_usd, provenance=UsageProvenance.CLI_REPORTED),
    )
    store.save_result(result)
    return task, result


def write_budget_yaml(tmp_path, window: str, limit_usd: float = 5.0) -> pathlib.Path:
    p = tmp_path / "budget.yaml"
    p.write_text(
        f"version: 0.3\n"
        f"budgets:\n"
        f"  global:\n"
        f"    limit_usd: {limit_usd}\n"
        f"    window: {window}\n"
        f"    warning_percent: 70\n"
        f"    critical_percent: 90\n"
        f"    hard_stop_percent: 100\n"
        f"  projects: {{}}\n"
        f"actions: {{}}\n",
        encoding="utf-8",
    )
    return p


def test_no_history_gives_unknown_budget_status(tmp_path):
    store = make_store(tmp_path)
    manager = UsageManager(store)
    status = manager.check_budget("proj")
    assert status.level == "unknown"
    assert status.pct_used is None


def test_budget_ok_below_warning(tmp_path):
    store = make_store(tmp_path)
    seed_task_with_cost(store, "proj", "claude_code", 0.10)
    manager = UsageManager(store)
    status = manager.check_budget("proj")
    assert status.total_cost_usd == 0.10
    assert status.level == "ok"


def test_budget_crosses_thresholds(tmp_path):
    store = make_store(tmp_path)
    # default limit_usd is 5.0 in usage_budget.example.yaml; 4.6 -> 92% -> critical
    seed_task_with_cost(store, "proj", "claude_code", 4.6)
    manager = UsageManager(store)
    status = manager.check_budget("proj")
    assert status.level == "critical"
    assert "prefer_lower_pressure_agent" in status.actions


def test_pressure_zero_when_no_cost_data(tmp_path):
    store = make_store(tmp_path)
    manager = UsageManager(store)
    assert manager.pressure("proj") == 0.0


def test_pressure_scales_with_usage(tmp_path):
    store = make_store(tmp_path)
    seed_task_with_cost(store, "proj", "claude_code", 2.5)  # 50% of 5.0
    manager = UsageManager(store)
    assert 0.45 < manager.pressure("proj") < 0.55


def test_project_scoping_isolates_costs(tmp_path):
    store = make_store(tmp_path)
    seed_task_with_cost(store, "proj_a", "claude_code", 1.0)
    seed_task_with_cost(store, "proj_b", "claude_code", 2.0)
    manager = UsageManager(store)
    assert manager.get_project_usage("proj_a")["total_cost_usd"] == 1.0
    assert manager.get_project_usage("proj_b")["total_cost_usd"] == 2.0


def test_window_since_all_time_returns_none():
    assert _window_since("all_time") is None
    assert _window_since("unrecognized") is None


def test_window_since_daily_is_start_of_today_utc():
    since = _window_since("daily")
    dt = datetime.fromisoformat(since)
    now = datetime.now(timezone.utc)
    assert dt.date() == now.date()
    assert dt.hour == 0 and dt.minute == 0 and dt.second == 0


def test_window_since_monthly_is_start_of_this_month_utc():
    since = _window_since("monthly")
    dt = datetime.fromisoformat(since)
    now = datetime.now(timezone.utc)
    assert dt.year == now.year and dt.month == now.month and dt.day == 1


def test_daily_window_excludes_old_cost_includes_todays(tmp_path):
    store = make_store(tmp_path)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    today = datetime.now(timezone.utc).isoformat()
    seed_task_with_cost(store, "proj", "claude_code", 4.0, finished_at=yesterday)
    seed_task_with_cost(store, "proj", "claude_code", 0.5, finished_at=today)

    budget_path = write_budget_yaml(tmp_path, "daily")
    manager = UsageManager(store, budget_path=budget_path)
    status = manager.check_budget("proj")
    assert status.total_cost_usd == 0.5  # only today's, old 4.0 excluded
    assert status.window == "daily"


def test_all_time_window_includes_everything(tmp_path):
    store = make_store(tmp_path)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    today = datetime.now(timezone.utc).isoformat()
    seed_task_with_cost(store, "proj", "claude_code", 4.0, finished_at=yesterday)
    seed_task_with_cost(store, "proj", "claude_code", 0.5, finished_at=today)

    budget_path = write_budget_yaml(tmp_path, "all_time")
    manager = UsageManager(store, budget_path=budget_path)
    status = manager.check_budget("proj")
    assert status.total_cost_usd == 4.5
