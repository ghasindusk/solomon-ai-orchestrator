"""Octavryn SI v0.5 R1-R4: schemas, adapter contract, adapter registry,
capability graph, intelligence registry, skill registry, capability-first
routing. Spec 12 "Skill tests" and the environment matrix rows that do not
need governance (governance rows are in test_v05_governance.py)."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from solomon.adapters import registry as adapter_registry
from solomon.adapters.base import AdapterHealth, AgentAdapter, Unsupported, is_unsupported
from solomon.capability_graph import CapabilityGraph
from solomon.descriptors import (
    Availability,
    CapabilityEvidence,
    EvidenceState,
    IntelligenceDescriptor,
    Locality,
    SchemaVersionError,
    SkillDescriptor,
    SkillScope,
    SurfaceDescriptor,
    check_schema_version,
)
from solomon.intelligence_registry import IntelligenceRegistry, discover_mcp_servers
from solomon.models import Task
from solomon.result import TaskResult
from solomon.router import Router
from solomon.skills import SkillRegistry
from solomon.state import StateStore


def make_task(role="coder", project_id="p1"):
    return Task(goal_id="g", project_id=project_id, type="t", role=role, definition_of_done=["result_recorded"])


def graph_with(*descs):
    g = CapabilityGraph.load()
    for d in descs:
        g.add_intelligence(d)
    return g


def intel(id_, caps, state=EvidenceState.DECLARED, locality=Locality.CLOUD, availability=Availability.AVAILABLE, provider="x"):
    return IntelligenceDescriptor(
        id=id_, display_name=id_, adapter_type="cli", provider=provider, locality=locality,
        availability=availability, capabilities=[CapabilityEvidence(c, state, "test") for c in caps],
    )


# --- R1 schemas -----------------------------------------------------------

def test_schema_version_accepts_05x_and_rejects_others():
    assert check_schema_version("0.5") == "0.5"
    assert check_schema_version("0.5.3") == "0.5.3"
    for bad in ("0.4", "1.0", "", None):
        with pytest.raises(SchemaVersionError):
            check_schema_version(bad)


def test_intelligence_descriptor_round_trip_keeps_unknown_states():
    d = IntelligenceDescriptor(id="a", display_name="A", adapter_type="cli")
    back = IntelligenceDescriptor.from_dict(d.to_dict())
    assert back.availability == Availability.UNKNOWN
    assert back.locality == Locality.UNKNOWN
    assert back == d


def test_intelligence_descriptor_rejects_v04_shape():
    data = IntelligenceDescriptor(id="a", display_name="A", adapter_type="cli").to_dict()
    data["schema_version"] = "0.4"
    with pytest.raises(SchemaVersionError):
        IntelligenceDescriptor.from_dict(data)


def test_skill_descriptor_validation():
    with pytest.raises(ValueError):
        SkillDescriptor(id="s", version=0, name="s")
    with pytest.raises(ValueError):
        SkillDescriptor(id="s", version=1, name="s", risk="SILLY")
    with pytest.raises(ValueError):
        SkillDescriptor(id="s", version=1, name="s", locality_constraint="moon")


def test_surface_never_has_privileged_execution():
    s = SurfaceDescriptor(id="x", type="desktop_app", permissions=["everything"])
    assert s.privileged_execution is False
    assert s.to_dict()["privileged_execution"] is False


# --- R4 adapter contract / registry ------------------------------------------

class _Bare(AgentAdapter):
    name = "bare"

    def health(self):
        return AdapterHealth(True)

    def execute(self, task, prompt, timeout_s=600):  # pragma: no cover
        raise AssertionError("discovery must never execute")


def test_contract_defaults_are_explicit_unsupported_not_success():
    a = _Bare()
    for op in (a.describe(), a.list_models(), a.probe_capability("coding"), a.prepare_request(make_task(), "p"),
               a.normalize_result({}, make_task()), a.normalize_usage({}), a.cleanup(), a.discover()):
        assert is_unsupported(op)
        assert not op  # never truthy


def test_builtin_adapters_declare_contract_and_discover_without_executing(monkeypatch):
    for name in ("claude_code", "codex", "antigravity", "localai_ollama"):
        a = adapter_registry.load_adapter(name)
        monkeypatch.setattr(a, "health", lambda: AdapterHealth(False, "offline for test"))
        monkeypatch.setattr(a, "execute", lambda *a_, **k: pytest.fail("discover executed"))
        decl = a.describe()
        assert decl.name == name and decl.capabilities
        d = a.discover()
        assert d.availability == Availability.UNAVAILABLE
        assert all(c.state == EvidenceState.DECLARED for c in d.capabilities)


def test_disabled_adapter_is_missing_and_fails_cleanly():
    adapter_registry.set_disabled({"codex"})
    try:
        a = adapter_registry.load_adapter("codex")
        assert a.health().available is False
        r = a.execute(make_task(), "x")
        assert r.status == "FAILED"
        assert "unavailable" in " ".join(r.uncertainties)
    finally:
        adapter_registry.set_disabled(set())


def test_broken_integration_import_does_not_break_core(monkeypatch):
    monkeypatch.setitem(adapter_registry._BUILTINS, "ghost", (".does_not_exist", "Nope", False))
    a = adapter_registry.load_adapter("ghost")
    assert a.health().available is False


def test_unknown_adapter_still_raises_value_error():
    with pytest.raises(ValueError):
        adapter_registry.load_adapter("definitely_not_real")


# --- R2 capability graph / intelligence registry -------------------------------

def test_aliases_normalize_and_unknown_names_are_kept():
    g = CapabilityGraph.load()
    assert g.normalize("local_rag") == "rag"
    assert g.normalize("Java_Coding") == "coding"
    assert g.normalize("quantum_telepathy") == "quantum_telepathy"
    assert g.unknown_capabilities(["coding", "quantum_telepathy"]) == ["quantum_telepathy"]


def test_implied_capability_never_stronger_than_declared():
    g = graph_with(intel("a", ["mcp_use"], state=EvidenceState.USER_CONFIRMED))
    ev = g.evidence_for("a")
    assert ev["mcp_use"] == EvidenceState.USER_CONFIRMED
    assert ev["tool_use"] == EvidenceState.DECLARED


def test_providers_prefer_verified_evidence():
    g = graph_with(intel("declared", ["coding"]), intel("verified", ["coding"], EvidenceState.HISTORICALLY_VERIFIED))
    assert [m.intelligence_id for m in g.providers_for("coding")] == ["verified", "declared"]


def test_unavailable_intelligence_not_a_provider_by_default():
    g = graph_with(intel("gone", ["coding"], availability=Availability.ABSENT))
    assert g.providers_for("coding") == []
    assert len(g.providers_for("coding", include_unavailable=True)) == 1


def _registry_with_fake(tmp_path, monkeypatch, available=True):
    class Fake(AgentAdapter):
        name = "fake"
        from solomon.adapters.base import AdapterDeclaration as _D
        declaration = _D(name="fake", display_name="Fake", adapter_type="cli", provider="p",
                         locality=Locality.LOCAL, capabilities=["coding"])

        def health(self):
            return AdapterHealth(available, "ok" if available else "down")

        def execute(self, task, prompt, timeout_s=600):  # pragma: no cover
            raise AssertionError

    adapter_registry.register_adapter("fake", lambda cwd=None: Fake())
    store = StateStore(tmp_path / "s.db")
    return IntelligenceRegistry(state=store), store


def test_registry_marks_disappeared_intelligence_absent_but_keeps_identity(tmp_path, monkeypatch):
    reg, store = _registry_with_fake(tmp_path, monkeypatch)
    try:
        reg.discover(["fake"])
        assert store.list_intelligences()[0]["availability"] == "available"
        IntelligenceRegistry(state=store).discover([])  # provider disappeared
        rows = store.list_intelligences()
        assert rows[0]["intelligence_id"] == "fake"
        assert rows[0]["availability"] == "absent"
        restored = IntelligenceRegistry(state=store)
        restored.load_persisted()
        assert restored.graph.providers_for("coding") == []
    finally:
        adapter_registry.unregister_adapter("fake")


def test_historical_verification_requires_complete_not_claimed_success(tmp_path, monkeypatch):
    reg, store = _registry_with_fake(tmp_path, monkeypatch)
    try:
        for i in range(3):
            t = make_task()
            t.status = t.status.__class__("RESULT_RECEIVED")
            store.save_task(t)
            store.save_result(TaskResult(task_id=t.task_id, status="RESULT_RECEIVED", summary="", agent="fake",
                                         started_at="2026-09-24T00:00:00+00:00", finished_at="2026-09-24T00:00:01+00:00"))
        d = reg.describe("fake")
        assert d.capability_states()["coding"] == EvidenceState.DECLARED
        for t in store.list_tasks():
            task = Task.from_schema_dict(t)
            task.status = task.status.__class__("COMPLETE")
            store.save_task(task)
        d = reg.describe("fake")
        assert d.capability_states()["coding"] == EvidenceState.HISTORICALLY_VERIFIED
    finally:
        adapter_registry.unregister_adapter("fake")


def test_user_confirmation_is_evidence_not_permission(tmp_path, monkeypatch):
    reg, store = _registry_with_fake(tmp_path, monkeypatch)
    try:
        store.confirm_capability("fake", "testing", confirmed_by="human:owner")
        d = reg.describe("fake")
        assert d.capability_states()["testing"] == EvidenceState.USER_CONFIRMED
        assert d.requested_permissions == []
    finally:
        adapter_registry.unregister_adapter("fake")


def test_mcp_discovery_reads_names_only(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"mcpServers": {"alpha": {"command": "x", "env": {"API_KEY": "sekrit"}}}}', encoding="utf-8")
    tools = discover_mcp_servers(cfg)
    assert [t.id for t in tools] == ["mcp:alpha"]
    assert "sekrit" not in repr(tools)
    assert discover_mcp_servers(tmp_path / "missing.json") == []


# --- R3 skill registry ---------------------------------------------------------

def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_core_pack_has_a_skill_for_every_v04_role():
    from solomon.router import ROLE_CAPABILITY_MAP

    reg = SkillRegistry.default(include_user=False)
    for role, cap in ROLE_CAPABILITY_MAP.items():
        skill = reg.for_role(role)
        assert skill is not None, role
        assert skill.required_capabilities == [cap]


def test_scope_precedence_and_no_risk_downgrade(tmp_path):
    reg = SkillRegistry.default(include_user=False)
    _write(tmp_path / "proj" / "s.yaml",
           'schema_version: "0.5"\nid: role.environment_operator\nversion: 2\nname: mine\nrisk: LOW\n'
           'requires: {capabilities: [environment_operations]}\n')
    reg.load_dir(tmp_path / "proj", SkillScope.PROJECT)
    s = reg.get("role.environment_operator")
    assert s.scope == SkillScope.PROJECT and s.version == 2
    assert s.risk == "HIGH"  # core said HIGH; override may not lower it
    assert any(c.kind == "overridden" for c in reg.conflicts)


def test_duplicate_id_in_same_scope_fails_closed(tmp_path):
    reg = SkillRegistry()
    body = 'schema_version: "0.5"\nid: dup\nversion: 1\nname: d\nrequires: {capabilities: [coding]}\n'
    _write(tmp_path / "a.yaml", body)
    _write(tmp_path / "b.yaml", body)
    reg.load_dir(tmp_path, SkillScope.USER)
    assert reg.get("dup") is None
    assert [c.kind for c in reg.conflicts] == ["ambiguous"]


def test_invalid_skill_file_is_reported_not_fatal(tmp_path):
    _write(tmp_path / "bad.yaml", 'schema_version: "0.4"\nid: old\nversion: 1\n')
    _write(tmp_path / "good.yaml", 'schema_version: "0.5"\nid: good\nversion: 1\nname: g\nrequires: {capabilities: [coding]}\n')
    reg = SkillRegistry()
    reg.load_dir(tmp_path, SkillScope.USER)
    assert reg.get("good") is not None
    assert any(c.kind == "invalid" for c in reg.conflicts)


def test_permissions_are_never_granted_by_installation():
    s = SkillDescriptor(id="x", version=1, name="x", requested_permissions=["git_push", "network"], scope=SkillScope.USER)
    granted, denied = SkillRegistry.effective_permissions(s)
    assert granted == [] and denied == ["git_push", "network"]
    granted, denied = SkillRegistry.effective_permissions(s, grants={"x": ["network"]})
    assert granted == ["network"] and denied == ["git_push"]


def test_unavailable_tool_is_reported():
    s = SkillDescriptor(id="x", version=1, name="x", tools=["git", "definitely-not-a-tool"])
    assert SkillRegistry.check_tools(s, which=lambda t: None if "not" in t else "/bin/" + t).missing == ["definitely-not-a-tool"]


def test_verification_maps_to_dod_and_unknown_stays_unverifiable():
    s = SkillDescriptor(id="x", version=1, name="x", verification=["build", "tests"])
    assert SkillRegistry.definition_of_done(s) == ["result_recorded", "build_passes", "skill_verification:tests"]


def test_disabled_skill_is_not_resolved():
    reg = SkillRegistry()
    reg.add_skill(SkillDescriptor(id="off", version=1, name="off", enabled=False, required_capabilities=["coding"]))
    assert reg.get("off") is None


def test_example_pack_loads(tmp_path):
    from solomon.skills import load_pack_file

    root = pathlib.Path(__file__).resolve().parents[1]
    pack = load_pack_file(root / "skill_packs" / "examples" / "minecraft_mod_development" / "pack.yaml", SkillScope.USER)
    assert pack.skills[0].tools == ["git", "java", "gradle"]


# --- capability-first routing ----------------------------------------------------

def test_router_v04_candidates_unchanged_by_capability_first_resolution():
    r = Router()
    assert set(r.candidates_for_role("coder")) == {"claude_code", "codex", "antigravity"}
    assert r.candidates_for_role("knowledge_curator") == ["localai_ollama"]
    assert r.candidates_for_role("not_a_role") == []


def test_missing_capability_skill_has_no_candidates_and_explains_why():
    r = Router()
    skill = SkillDescriptor(id="s", version=1, name="s", required_capabilities=["vision"])
    cands = r.resolve_candidates(make_task(), skill=skill)
    assert not any(c.eligible for c in cands)
    assert all("vision" in c.missing for c in cands)


def test_locality_constraint_excludes_cloud():
    g = graph_with(intel("cloud", ["coding"]), intel("local", ["coding"], locality=Locality.LOCAL))
    r = Router(capability_graph=g)
    skill = SkillDescriptor(id="s", version=1, name="s", required_capabilities=["coding"], locality_constraint="local")
    assert r.candidates_for_role("coder", skill=skill) == ["local"]


def test_verified_evidence_outscores_declared_all_else_equal():
    g = graph_with(intel("declared", ["coding"]), intel("verified", ["coding"], EvidenceState.HISTORICALLY_VERIFIED))
    r = Router(capability_graph=g)
    health = {"declared": AdapterHealth(True), "verified": AdapterHealth(True)}
    scores = r.route(make_task(), health_checks=health)
    assert [s.adapter_name for s in scores] == ["verified", "declared"]


def test_absent_provider_after_registration_is_not_routed():
    g = graph_with(intel("a", ["coding"]), intel("b", ["coding"]))
    r = Router(capability_graph=g)
    assert set(r.candidates_for_role("coder")) == {"a", "b"}
    g._intelligences["b"].availability = Availability.ABSENT
    assert r.candidates_for_role("coder") == ["a"]
