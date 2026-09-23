"""Cross-cutting integration tests for the Phase 6 reopen (Token & Compute
Intelligence, DECISIONS.md D27-D29), step 8/10: double-counting,
provenance, cross-project leakage, budget, Router fallback, aggregation
and routing, exercised end-to-end against a real StateStore rather than
hand-built UsageRecord lists (those already have per-module unit coverage
in test_usage_record.py/test_usage_aggregation.py/test_token_budget.py/
test_token_efficiency.py/test_router.py)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth
from solomon.models import Task, TaskStatus
from solomon.result import TaskResult, Usage, UsageProvenance
from solomon.router import Router
from solomon.state import StateStore
from solomon.token_budget import TokenBudgetManager
from solomon.token_efficiency import compute_token_efficiency
from solomon.usage import UsageManager
from solomon.usage_aggregation import aggregate_usage_records


def make_store(tmp_path):
    return StateStore(db_path=tmp_path / "state.sqlite3")


def seed_task(store, project_id, **task_overrides):
    defaults = dict(goal_id="g1", project_id=project_id, type="t", role="coder", definition_of_done=["x"])
    defaults.update(task_overrides)
    task = Task(**defaults)
    store.save_task(task)
    return task


def seed_result(store, task, agent="claude_code", input_tokens=100, output_tokens=50,
                 provenance=UsageProvenance.API_REPORTED, cost_usd=None, **overrides):
    defaults = dict(
        task_id=task.task_id,
        status="RESULT_RECEIVED",
        summary="ok",
        agent=agent,
        started_at="2026-09-23T00:00:00+00:00",
        finished_at="2026-09-23T00:00:02+00:00",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd, provenance=provenance),
    )
    defaults.update(overrides)
    result = TaskResult(**defaults)
    store.save_result(result)
    # Real execution (execution.py) syncs tasks.status after a result comes
    # back via task_status_from_result_status(); replicate that here so
    # StateStore.list_tasks(statuses=[...]) reflects it, same as production.
    from solomon.models import task_status_from_result_status
    task.status = task_status_from_result_status(result.status)
    store.save_task(task)
    return result


def write_token_budget_yaml(tmp_path, limit_tokens=1000):
    import yaml

    p = tmp_path / "token_budget.yaml"
    config = {
        "version": 0.4,
        "budgets": {
            "global": {
                "limit_tokens": limit_tokens,
                "window": "all_time",
                "warning_percent": 70,
                "critical_percent": 90,
                "hard_stop_percent": 100,
            },
            "scopes": {"project": {}, "agent": {}, "model": {}, "goal": {}, "task": {}},
        },
        "actions": {"warning": ["log"], "critical": ["compress_context"], "hard_stop": ["pause_or_request_approval"]},
    }
    p.write_text(yaml.safe_dump(config), encoding="utf-8")
    return p


def test_retried_task_produces_two_records_not_a_doubled_or_deduped_one(tmp_path):
    store = make_store(tmp_path)
    task = seed_task(store, "proj-1")
    seed_result(store, task, input_tokens=80, output_tokens=20, finished_at="2026-09-23T00:00:01+00:00")
    seed_result(store, task, input_tokens=100, output_tokens=40, finished_at="2026-09-23T00:00:05+00:00")

    records = store.get_usage_records(project_id="proj-1")
    assert len(records) == 2

    buckets = aggregate_usage_records(records, "task_id")
    assert len(buckets) == 1
    assert buckets[0].total_tokens == (80 + 20) + (100 + 40)


def test_two_independent_tasks_are_not_merged_into_one_count(tmp_path):
    store = make_store(tmp_path)
    t1 = seed_task(store, "proj-1")
    t2 = seed_task(store, "proj-1")
    seed_result(store, t1, input_tokens=50, output_tokens=0)
    seed_result(store, t2, input_tokens=50, output_tokens=0)

    records = store.get_usage_records(project_id="proj-1")
    assert len(records) == 2
    buckets = {b.key: b.total_tokens for b in aggregate_usage_records(records, "task_id")}
    assert buckets[t1.task_id] == 50
    assert buckets[t2.task_id] == 50


def test_provenance_survives_state_roundtrip_and_flows_into_aggregation(tmp_path):
    store = make_store(tmp_path)
    t1 = seed_task(store, "proj-1")
    t2 = seed_task(store, "proj-1")
    seed_result(store, t1, provenance=UsageProvenance.API_REPORTED)
    seed_result(store, t2, provenance=UsageProvenance.ESTIMATED)

    records = store.get_usage_records(project_id="proj-1")
    provenances = {r.task_id: r.provenance for r in records}
    assert provenances[t1.task_id] == UsageProvenance.API_REPORTED
    assert provenances[t2.task_id] == UsageProvenance.ESTIMATED

    bucket = aggregate_usage_records(records, "project_id")[0]
    assert bucket.provenance_counts == {"API_REPORTED": 1, "ESTIMATED": 1}
    assert bucket.has_estimated is True


def test_no_cross_project_leakage_in_usage_records_aggregation_or_budget(tmp_path):
    store = make_store(tmp_path)
    task_a = seed_task(store, "proj-a")
    task_b = seed_task(store, "proj-b")
    seed_result(store, task_a, input_tokens=100, output_tokens=0)
    seed_result(store, task_b, input_tokens=900, output_tokens=0)

    records_a = store.get_usage_records(project_id="proj-a")
    assert len(records_a) == 1
    assert records_a[0].project_id == "proj-a"

    all_records = store.get_usage_records()
    buckets = {b.key: b.total_tokens for b in aggregate_usage_records(all_records, "project_id")}
    assert buckets["proj-a"] == 100
    assert buckets["proj-b"] == 900

    manager = TokenBudgetManager(write_token_budget_yaml(tmp_path, limit_tokens=1000))
    status_a = manager.check(all_records, scope="project", scope_id="proj-a")
    assert status_a.total_tokens == 100


def test_budget_hard_stop_reachable_from_real_state_data(tmp_path):
    store = make_store(tmp_path)
    task = seed_task(store, "proj-1")
    seed_result(store, task, input_tokens=800, output_tokens=200)

    records = store.get_usage_records(project_id="proj-1")
    manager = TokenBudgetManager(write_token_budget_yaml(tmp_path, limit_tokens=1000))
    status = manager.check(records, scope="project", scope_id="proj-1")
    assert status.level == "hard_stop"
    assert "pause_or_request_approval" in status.actions


def test_router_falls_back_to_usd_when_token_budget_status_is_unknown(tmp_path):
    store = make_store(tmp_path)
    task_seed = seed_task(store, "proj-fallback")
    store.save_result(
        TaskResult(
            task_id=task_seed.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:02+00:00",
            usage=Usage(cost_usd=4.5, provenance=UsageProvenance.CLI_REPORTED),
        )
    )
    usage_manager = UsageManager(store)
    token_budget_manager = TokenBudgetManager(write_token_budget_yaml(tmp_path, limit_tokens=1000))
    router = Router(state=store, usage_manager=usage_manager, token_budget_manager=token_budget_manager)
    task = Task(goal_id="g1", project_id="proj-fallback", type="t", role="coder", definition_of_done=["x"])
    scored = router.score_adapter("claude_code", task, health=AdapterHealth(True))
    assert scored.components["usage_efficiency"] < 0.2
    assert any("legacy USD budget pressure" in n for n in scored.notes)


def test_aggregation_by_agent_and_project_stay_isolated_with_real_multi_agent_data(tmp_path):
    store = make_store(tmp_path)
    t1 = seed_task(store, "proj-a")
    t2 = seed_task(store, "proj-a")
    t3 = seed_task(store, "proj-b")
    seed_result(store, t1, agent="claude_code", input_tokens=100, output_tokens=0)
    seed_result(store, t2, agent="codex", input_tokens=200, output_tokens=0)
    seed_result(store, t3, agent="claude_code", input_tokens=999, output_tokens=0)

    records = store.get_usage_records()
    by_agent = {b.key: b.total_tokens for b in aggregate_usage_records(records, "agent_id")}
    assert by_agent["claude_code"] == 100 + 999
    assert by_agent["codex"] == 200

    by_project = {b.key: b.total_tokens for b in aggregate_usage_records(records, "project_id")}
    assert by_project["proj-a"] == 100 + 200
    assert by_project["proj-b"] == 999


def test_token_efficiency_distinguishes_verified_from_unverified_using_real_task_status(tmp_path):
    store = make_store(tmp_path)
    verified_task = seed_task(store, "proj-1")
    seed_result(store, verified_task, input_tokens=100, output_tokens=0)
    verified_task.status = TaskStatus.COMPLETE
    store.save_task(verified_task)

    unverified_task = seed_task(store, "proj-1")
    seed_result(store, unverified_task, input_tokens=900, output_tokens=0)

    records = store.get_usage_records(project_id="proj-1")
    success_ids = {t["task_id"] for t in store.list_tasks(project_id="proj-1", statuses=["RESULT_RECEIVED", "COMPLETE"])}
    verified_ids = {t["task_id"] for t in store.list_tasks(project_id="proj-1", statuses=["COMPLETE"])}

    eff = compute_token_efficiency(records, success_task_ids=success_ids, verified_task_ids=verified_ids, scope="project", scope_id="proj-1")
    assert eff.success_task_count == 2
    assert eff.verified_success_task_count == 1
    assert eff.tokens_per_success == (100 + 900) / 2
    assert eff.tokens_per_verified_success == 100


def test_usage_record_task_id_cross_references_event_store(tmp_path):
    store = make_store(tmp_path)
    task = seed_task(store, "proj-1")
    seed_result(store, task, input_tokens=100, output_tokens=0)

    records = store.get_usage_records(project_id="proj-1")
    assert len(records) == 1
    usage_task_id = records[0].task_id

    events = store.list_events(project_id="proj-1")
    event_task_ids = {e["task_id"] for e in events if e.get("task_id")}
    assert usage_task_id in event_task_ids
