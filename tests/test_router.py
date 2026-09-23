import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth
from solomon.models import Task
from solomon.router import Router, ROLE_CAPABILITY_MAP
from solomon.state import StateStore
from solomon.result import TaskResult, Usage, UsageProvenance
from solomon.token_budget import TokenBudgetManager
from solomon.usage import UsageManager
from solomon.usage_record import TokenUsage, UsageRecord


def make_task(role="coder", project_id="p1", context_budget=None) -> Task:
    return Task(
        goal_id="g1",
        project_id=project_id,
        type="test",
        role=role,
        definition_of_done=["x"],
        context_budget_tokens=context_budget,
    )


def test_candidates_for_role_matches_real_config():
    router = Router()
    coders = router.candidates_for_role("coder")
    assert "claude_code" in coders
    assert "codex" in coders
    reviewers = router.candidates_for_role("reviewer")
    assert "claude_code" in reviewers
    assert "codex" in reviewers
    knowledge_curators = router.candidates_for_role("knowledge_curator")
    assert "localai_ollama" in knowledge_curators


def test_unknown_role_returns_empty():
    router = Router()
    assert router.candidates_for_role("not_a_real_role") == []


def test_all_roles_in_map_resolve_to_at_least_one_candidate_or_none_gracefully():
    router = Router()
    for role in ROLE_CAPABILITY_MAP:
        # Should not raise; some roles may have zero candidates given the
        # current lean capability tags, and that's a valid (if notable) result.
        router.candidates_for_role(role)


def test_score_adapter_without_state_uses_neutral_placeholders():
    router = Router()
    score = router.score_adapter("claude_code", make_task(role="coder"), health=AdapterHealth(True))
    assert score.components["skill_match"] == 1.0
    assert score.components["availability"] == 1.0
    assert score.components["historical_quality"] == 0.5
    assert score.components["usage_efficiency"] == 0.5
    assert 0.0 <= score.total <= 1.0


def test_unavailable_adapter_scores_lower_than_available_one():
    router = Router()
    task = make_task(role="coder")
    available = router.score_adapter("claude_code", task, health=AdapterHealth(True))
    unavailable = router.score_adapter("codex", task, health=AdapterHealth(False))
    assert available.total > unavailable.total


def test_route_orders_by_total_descending():
    router = Router()
    task = make_task(role="coder")
    health_checks = {"claude_code": AdapterHealth(True), "codex": AdapterHealth(False)}
    scores = router.route(task, health_checks=health_checks)
    assert len(scores) >= 2
    totals = [s.total for s in scores]
    assert totals == sorted(totals, reverse=True)
    assert scores[0].adapter_name == "claude_code"


def test_historical_stats_feed_into_score(tmp_path):
    store = StateStore(db_path=tmp_path / "state.sqlite3")
    task = Task(
        goal_id="g1", project_id="proj", type="t", role="coder", definition_of_done=["x"]
    )
    store.save_task(task)
    result = TaskResult(
        task_id=task.task_id,
        status="RESULT_RECEIVED",
        summary="ok",
        agent="claude_code",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
    )
    store.save_result(result)

    router = Router(state=store)
    scored = router.score_adapter(
        "claude_code", make_task(role="coder", project_id="proj"), health=AdapterHealth(True)
    )
    assert scored.components["historical_quality"] == 1.0
    assert scored.components["project_experience"] == 0.2  # 1 sample / 5.0
    assert "historical_quality: no recorded task history yet" not in " ".join(scored.notes)


def test_local_free_adapter_always_gets_full_usage_efficiency(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    manager = UsageManager(store)
    router = Router(state=store, usage_manager=manager)
    scored = router.score_adapter("localai_ollama", make_task(role="coder"), health=AdapterHealth(True))
    assert scored.components["usage_efficiency"] == 1.0


def test_cloud_adapter_usage_efficiency_drops_under_budget_pressure(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task_seed = Task(goal_id="g1", project_id="pressure_proj", type="t", role="coder", definition_of_done=["x"])
    store.save_task(task_seed)
    store.save_result(
        TaskResult(
            task_id=task_seed.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:02+00:00",
            usage=Usage(cost_usd=4.5, provenance=UsageProvenance.CLI_REPORTED),  # 90% of default $5 limit
        )
    )
    manager = UsageManager(store)
    router = Router(state=store, usage_manager=manager)
    task = make_task(role="coder", project_id="pressure_proj")
    scored = router.score_adapter("claude_code", task, health=AdapterHealth(True))
    assert scored.components["usage_efficiency"] < 0.2
    assert any("budget pressure" in n for n in scored.notes)


def test_context_fitness_scales_down_when_budget_exceeds_window():
    router = Router()
    task = make_task(role="coder", context_budget=10_000_000)  # far beyond any known window
    scored = router.score_adapter("localai_ollama", task, health=AdapterHealth(True))
    assert scored.components["context_fitness"] < 1.0


# --- GPU-aware routing ---

def test_no_gpu_telemetry_does_not_penalize_local_adapter(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store, usage_manager=UsageManager(store))
    task = make_task(role="knowledge_curator")
    scored = router.score_adapter("localai_ollama", task, health=AdapterHealth(True), gpu_telemetry=None)
    assert scored.components["usage_efficiency"] == 1.0


def test_gpu_busy_penalizes_local_adapter():
    router = Router()
    task = make_task(role="knowledge_curator")
    gpu = {"name": "Test GPU", "gpu_load_percent": 95.0, "vram_used_mb": 8000, "vram_total_mb": 12000}
    scored = router.score_adapter("localai_ollama", task, health=AdapterHealth(True), gpu_telemetry=gpu)
    assert scored.components["usage_efficiency"] < 0.1
    assert any("GPU busy" in n for n in scored.notes)


def test_gpu_below_threshold_does_not_penalize(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    router = Router(state=store, usage_manager=UsageManager(store))
    task = make_task(role="knowledge_curator")
    gpu = {"name": "Test GPU", "gpu_load_percent": 10.0, "vram_used_mb": 500, "vram_total_mb": 12000}
    scored = router.score_adapter("localai_ollama", task, health=AdapterHealth(True), gpu_telemetry=gpu)
    assert scored.components["usage_efficiency"] == 1.0


def test_gpu_busy_never_penalizes_cloud_adapter():
    router = Router()
    task = make_task(role="coder")
    gpu = {"name": "Test GPU", "gpu_load_percent": 99.0, "vram_used_mb": 11000, "vram_total_mb": 12000}
    scored = router.score_adapter("claude_code", task, health=AdapterHealth(True), gpu_telemetry=gpu)
    assert scored.components["usage_efficiency"] == 0.5  # neutral placeholder, no UsageManager -- unaffected by GPU
    assert not any("GPU busy" in n for n in scored.notes)


def test_route_forwards_gpu_telemetry_and_lowers_busy_local_adapters_total():
    router = Router()
    task = make_task(role="knowledge_curator")
    health_checks = {name: AdapterHealth(True) for name in router.candidates_for_role("knowledge_curator")}

    idle_scores = router.route(task, health_checks=health_checks, gpu_telemetry=None)
    busy_scores = router.route(
        task, health_checks=health_checks,
        gpu_telemetry={"name": "Test GPU", "gpu_load_percent": 95.0, "vram_used_mb": 11000, "vram_total_mb": 12000},
    )
    idle_total = next(s.total for s in idle_scores if s.adapter_name == "localai_ollama")
    busy_total = next(s.total for s in busy_scores if s.adapter_name == "localai_ollama")
    assert busy_total < idle_total


def write_token_budget_yaml(tmp_path, limit_tokens: int = 1000) -> pathlib.Path:
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
        "actions": {},
    }
    import yaml
    p.write_text(yaml.safe_dump(config), encoding="utf-8")
    return p


def test_token_pressure_takes_priority_over_legacy_usage_manager(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    manager = UsageManager(store)  # would otherwise report 0 pressure (no cost data)
    router = Router(state=store, usage_manager=manager)
    task = make_task(role="coder")
    scored = router.score_adapter("claude_code", task, health=AdapterHealth(True), token_pressure=0.95)
    assert scored.components["usage_efficiency"] < 0.1
    assert any("Token budget pressure" in n for n in scored.notes)


def test_local_free_adapter_gets_full_efficiency_even_under_token_pressure():
    router = Router()
    task = make_task(role="coder")
    scored = router.score_adapter("localai_ollama", task, health=AdapterHealth(True), token_pressure=0.95)
    assert scored.components["usage_efficiency"] == 1.0


def test_no_signal_at_all_falls_back_to_neutral_with_updated_note():
    router = Router()
    scored = router.score_adapter("claude_code", make_task(role="coder"), health=AdapterHealth(True))
    assert scored.components["usage_efficiency"] == 0.5
    assert any("no TokenBudgetManager or UsageManager provided" in n for n in scored.notes)


def test_route_computes_token_pressure_from_token_budget_manager_and_state(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task_seed = Task(goal_id="g1", project_id="proj-tok", type="t", role="coder", definition_of_done=["x"])
    store.save_task(task_seed)
    store.save_result(
        TaskResult(
            task_id=task_seed.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:02+00:00",
            usage=Usage(input_tokens=600, output_tokens=300, provenance=UsageProvenance.API_REPORTED),  # 900/1000 = 90%
        )
    )
    token_budget_manager = TokenBudgetManager(write_token_budget_yaml(tmp_path, limit_tokens=1000))
    router = Router(state=store, token_budget_manager=token_budget_manager)
    task = make_task(role="coder", project_id="proj-tok")
    health_checks = {"claude_code": AdapterHealth(True)}
    scores = router.route(task, health_checks=health_checks)
    claude_score = next(s for s in scores if s.adapter_name == "claude_code")
    assert claude_score.components["usage_efficiency"] < 0.2
    assert any("Token budget pressure" in n for n in claude_score.notes)


def test_route_without_token_budget_manager_still_falls_back_to_usage_manager(tmp_path):
    store = StateStore(db_path=tmp_path / "s.sqlite3")
    task_seed = Task(goal_id="g1", project_id="pressure_proj2", type="t", role="coder", definition_of_done=["x"])
    store.save_task(task_seed)
    store.save_result(
        TaskResult(
            task_id=task_seed.task_id, status="RESULT_RECEIVED", summary="ok", agent="claude_code",
            started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:02+00:00",
            usage=Usage(cost_usd=4.5, provenance=UsageProvenance.CLI_REPORTED),
        )
    )
    manager = UsageManager(store)
    router = Router(state=store, usage_manager=manager)  # no token_budget_manager
    task = make_task(role="coder", project_id="pressure_proj2")
    scored = router.score_adapter("claude_code", task, health=AdapterHealth(True))
    assert scored.components["usage_efficiency"] < 0.2
    assert any("legacy USD budget pressure" in n for n in scored.notes)
