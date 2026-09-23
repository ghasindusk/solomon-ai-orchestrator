"""Diagnostics Export redaction/isolation tests (v0.4 Phase 8 UX,
DECISIONS.md D33). Formal Spec v0.4 section 21: diagnostics exports must
redact/omit prompts, source contents, secrets, private paths and
Obsidian contents unless the user explicitly includes them."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import AdapterHealth
from solomon.diagnostics import build_diagnostics_report
from solomon.registry import ProjectEntry, ProjectRegistry
from solomon.state import StateStore

_SECRET = "sk-" + "A" * 25


def make_store(tmp_path):
    return StateStore(db_path=tmp_path / "state.sqlite3")


def write_registry(tmp_path, projects):
    import yaml

    projects_dict = {}
    for p in projects:
        entry = {k: v for k, v in vars(p).items() if k != "project_id" and v is not None}
        projects_dict[p.project_id] = entry

    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"projects": projects_dict}), encoding="utf-8")
    return ProjectRegistry(registry_path=path)


def make_project(project_id, tmp_path):
    knowledge_dir = tmp_path / (project_id + "_knowledge")
    knowledge_dir.mkdir(exist_ok=True)
    return ProjectEntry(
        project_id=project_id,
        name="Project " + project_id,
        repo_path=str(tmp_path / (project_id + "_repo")),
        knowledge_path=str(knowledge_dir),
        status="active",
    )


def seed_approval_with_secret(store, project_id):
    store.create_approval_request(
        request_id="appr-" + project_id,
        project_id=project_id,
        risk="HIGH",
        reason="do the thing with key " + _SECRET + " please",
        task_params={"prompt": "use " + _SECRET + " to authenticate", "adapter": "claude_code"},
    )


def test_default_mode_excludes_paths_reasons_and_event_detail(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])
    seed_approval_with_secret(store, "proj-a")
    store.log_event(project_id="proj-a", event="something_happened", detail="sensitive free text here")

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a")
    d = report.to_dict()

    proj = d["projects"][0]
    assert "repo_path" not in proj
    assert "knowledge_path" not in proj
    assert "knowledge_note_titles" not in proj
    assert d["approvals"] is None
    assert d["approvals_summary"] == {"pending:HIGH": 1}
    for e in d["recent_events"]:
        assert "detail" not in e


def test_include_sensitive_redacts_secrets_within_included_text(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])
    seed_approval_with_secret(store, "proj-a")

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", include_sensitive=True)
    d = report.to_dict()

    assert d["approvals"] is not None
    approval = d["approvals"][0]
    assert _SECRET not in approval["reason"]
    assert "[REDACTED]" in approval["reason"]
    assert _SECRET not in approval["task_params"]["prompt"]
    assert "[REDACTED]" in approval["task_params"]["prompt"]


def test_include_sensitive_exposes_real_paths(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", include_sensitive=True)
    proj = report.to_dict()["projects"][0]
    assert proj["repo_path"] == project.repo_path
    assert proj["knowledge_path"] == project.knowledge_path


def test_event_detail_included_and_redacted_only_when_sensitive(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])
    store.log_event(project_id="proj-a", event="thing", detail="leaked " + _SECRET)

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", include_sensitive=True)
    events = report.to_dict()["recent_events"]
    assert len(events) == 1
    assert _SECRET not in events[0]["detail"]
    assert "[REDACTED]" in events[0]["detail"]


def test_cross_project_isolation_in_diagnostics_report(tmp_path):
    store = make_store(tmp_path)
    project_a = make_project("proj-a", tmp_path)
    project_b = make_project("proj-b", tmp_path)
    registry = write_registry(tmp_path, [project_a, project_b])
    seed_approval_with_secret(store, "proj-b")
    store.log_event(project_id="proj-b", event="b_only_event")

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", include_sensitive=True)
    d = report.to_dict()

    assert len(d["projects"]) == 1
    assert d["projects"][0]["project_id"] == "proj-a"
    assert d["approvals_summary"] == {}
    assert d["approvals"] == []
    assert all(e["event"] != "b_only_event" for e in d["recent_events"])


def test_no_project_id_scans_all_projects_but_each_stays_isolated(tmp_path):
    store = make_store(tmp_path)
    project_a = make_project("proj-a", tmp_path)
    project_b = make_project("proj-b", tmp_path)
    registry = write_registry(tmp_path, [project_a, project_b])

    report = build_diagnostics_report(store, registry, {})
    ids = {p["project_id"] for p in report.to_dict()["projects"]}
    assert ids == {"proj-a", "proj-b"}


def test_note_bodies_never_included_even_when_sensitive(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    knowledge_dir = pathlib.Path(project.knowledge_path)
    (knowledge_dir / "note.md").write_text(
        "---\ntitle: My Note\n---\nThis body contains " + _SECRET + " and must never be exported.",
        encoding="utf-8",
    )
    registry = write_registry(tmp_path, [project])

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", include_sensitive=True)
    serialized = str(report.to_dict())
    assert _SECRET not in serialized
    assert "must never be exported" not in serialized
    assert "My Note" in serialized


def test_note_titles_excluded_by_default(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    knowledge_dir = pathlib.Path(project.knowledge_path)
    (knowledge_dir / "note.md").write_text("---\ntitle: Secret Project Name\n---\nbody", encoding="utf-8")
    registry = write_registry(tmp_path, [project])

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a")
    serialized = str(report.to_dict())
    assert "Secret Project Name" not in serialized


def test_agent_health_detail_redacted(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])

    health_checks = {"claude_code": AdapterHealth(True, detail="authenticated with " + _SECRET)}
    report = build_diagnostics_report(store, registry, health_checks, project_id="proj-a")
    agent = report.to_dict()["agents"][0]
    assert _SECRET not in agent["detail"]
    assert "[REDACTED]" in agent["detail"]


def test_event_limit_is_respected(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])
    for i in range(10):
        store.log_event(project_id="proj-a", event="event_" + str(i))

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", event_limit=3)
    events = report.to_dict()["recent_events"]
    assert len(events) == 3
    assert [e["event"] for e in events] == ["event_7", "event_8", "event_9"]


def test_usage_and_gpu_are_always_included_as_safe_aggregate_data(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])
    gpu = {"name": "Test GPU", "gpu_load_percent": 12.0, "vram_used_mb": 500, "vram_total_mb": 8000}

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a", gpu_telemetry=gpu)
    d = report.to_dict()
    assert d["gpu"] == gpu
    assert "usage" in d["projects"][0]


def test_environment_contains_no_local_paths(tmp_path):
    store = make_store(tmp_path)
    project = make_project("proj-a", tmp_path)
    registry = write_registry(tmp_path, [project])

    report = build_diagnostics_report(store, registry, {}, project_id="proj-a")
    env = report.to_dict()["environment"]
    assert str(tmp_path) not in str(env)
    assert set(env.keys()) == {"python_version", "platform"}
