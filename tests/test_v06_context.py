"""Offline Phase 1A boundary, replay, migration and concurrent-writer tests."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from solomon.context.contracts import (RECORD_TYPES, ContractError, IntakeEnvelope,
    EvidenceReference, Proposal, KnowledgeRevision, OutcomeContract, VerificationReport,
    MemoryMigrationManifest, canonical, digest, validator)
from solomon.context.store import ContextRepository, PrincipalScope, ConflictError
from solomon.context.verifier_config import parameters_for
from solomon.models import Task
from solomon.registry import ProjectRegistry
from solomon.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "12_v0.6_Context_Decision"
EXAMPLES = ROOT / "examples" / "v06_context"
MANIFEST = json.loads((EXAMPLES / "manifest.json").read_text())
SCOPE = PrincipalScope("tester", frozenset({"demo", "other"}))
CAPTURE = (EXAMPLES / "capture.txt").read_bytes()


def sample(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    registry_path.write_text("projects:\n  demo: {}\n  other: {}\n", encoding="utf-8")
    store = StateStore(tmp_path / "state.sqlite3")
    repository = ContextRepository(store, ProjectRegistry(registry_path))
    repository.initialize()
    yield repository
    store.close()


def add_evidence(repo, project="demo", eid="evidence-demo-1", attempt=None):
    value = sample("evidence.valid.json")
    value.update(project_id=project, evidence_id=eid, attempt_id=attempt)
    record = EvidenceReference.from_dict(value)
    repo.append(project, SCOPE, record, "capture-" + eid, captured_bytes=CAPTURE)
    return record


def add_proposal(repo):
    add_evidence(repo)
    proposal = Proposal.from_dict(sample("proposal.valid.json"))
    repo.append("demo", SCOPE, proposal, "propose")
    return proposal


@pytest.mark.parametrize("fixture", MANIFEST, ids=lambda f: f["file"])
def test_structural_fixtures(fixture):
    kind = fixture["schema"].removesuffix(".schema.json")
    assert validator(kind).is_valid(sample(fixture["file"])) is fixture["structural"]


@pytest.mark.parametrize("kind", RECORD_TYPES)
def test_runtime_schema_matches_phase0(kind):
    if not DOCS.exists():
        pytest.skip("private Phase 0 documents are omitted from publication candidate")
    assert (ROOT / "src/solomon/context/schemas" / (kind + ".schema.json")).read_bytes() == (
        DOCS / "schemas" / (kind + ".schema.json")).read_bytes()


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1.0}',
                                  '{"x":Infinity}', '[]', '{"x":"\\ud800"}',
                                  '{"x":"' + 'a' * 262144 + '"}'],
                         ids=["duplicate", "nan", "float", "infinite", "array", "surrogate", "oversized"])
def test_hostile_json_rejected(raw):
    with pytest.raises(ContractError):
        IntakeEnvelope(raw)


@pytest.mark.parametrize("raw", [None, 1, {}, []])
def test_non_json_text_input_rejected(raw):
    with pytest.raises(ContractError):
        IntakeEnvelope(raw)


def test_immutable_defensive_copy():
    record = Proposal.from_dict(sample("proposal.valid.json"))
    value = record.data
    value["evidence_ids"].clear()
    assert record.data["evidence_ids"] == ["evidence-demo-1"]
    with pytest.raises(AttributeError):
        record.raw = "{}"


@pytest.mark.parametrize("mutation", ["digest", "duplicate", "version_bool", "id", "extra"])
def test_contract_tampering(mutation):
    value = sample("contract.valid.json")
    if mutation == "digest":
        value["criteria"][0]["expectation"] = "pretend success"
    elif mutation == "duplicate":
        value["criteria"].append(value["criteria"][0].copy())
    elif mutation == "version_bool":
        value["version"] = True
    elif mutation == "id":
        value["task_id"] = " "
    else:
        value["approval"] = True
    with pytest.raises(ContractError):
        OutcomeContract.from_dict(value)


@pytest.mark.parametrize("field", ["captured_at", "source_created_at"])
def test_time_validation(field):
    value = sample("intake.valid.json")
    value[field] = "2026-99-99T00:00:00Z" if field == "captured_at" else "2099-01-01T00:00:00Z"
    with pytest.raises(ContractError):
        IntakeEnvelope.from_dict(value)


def test_scope_missing_reference_and_rollback(repo):
    proposal = Proposal.from_dict(sample("proposal.valid.json"))
    add_evidence(repo, project="other")
    with pytest.raises(ContractError):
        repo.append("demo", SCOPE, proposal, "propose")
    assert repo.store.conn.execute("SELECT COUNT(*) FROM v06_operations WHERE project_id='demo'").fetchone()[0] == 0
    add_evidence(repo)
    assert repo.append("demo", SCOPE, proposal, "propose")
    with pytest.raises(PermissionError):
        repo.get("demo", PrincipalScope("tester", frozenset({"other"})), "proposal", proposal.record_id)
    with pytest.raises(PermissionError):
        repo.get("unregistered", PrincipalScope("tester", frozenset({"unregistered"})), "proposal", "x")
    with pytest.raises(PermissionError):
        repo.append("other", SCOPE, proposal, "cross-project")


def test_inactive_project_is_read_only(tmp_path):
    registry_path = tmp_path / "projects.yaml"
    registry_path.write_text("projects:\n  demo:\n    status: superseded\n", encoding="utf-8")
    store = StateStore(tmp_path / "state.sqlite3")
    repository = ContextRepository(store, ProjectRegistry(registry_path))
    repository.initialize()
    scope = PrincipalScope("tester", frozenset({"demo"}))
    assert repository.get("demo", scope, "proposal", "missing") is None
    with pytest.raises(PermissionError):
        repository.append("demo", scope, Proposal.from_dict(sample("proposal.valid.json")), "write")
    store.close()


@pytest.mark.parametrize("principal,projects", [("", {"demo"}), ("p", "demo"),
                                                 ("p", {"bad\nproject"}), ("x" * 129, {"demo"})])
def test_principal_scope_is_bounded(principal, projects):
    with pytest.raises(ValueError):
        PrincipalScope(principal, projects)


def test_capture_bytes_are_required(repo):
    record = IntakeEnvelope.from_dict(sample("intake.valid.json"))
    for raw in (None, b"forged", b"x" * 1048577):
        with pytest.raises(ContractError):
            repo.append("demo", SCOPE, record, "capture", captured_bytes=raw)
    assert repo.append("demo", SCOPE, record, "capture", captured_bytes=CAPTURE)


def test_immutable_records_and_idempotent_operation(repo):
    proposal = add_proposal(repo)
    assert repo.append("demo", SCOPE, proposal, "propose") is False
    changed = proposal.data
    changed["assertion"] = "A different proposal with the same operation ID."
    changed["content_sha256"] = digest(changed["assertion"].encode("utf-8"))
    with pytest.raises(ConflictError):
        repo.append("demo", SCOPE, Proposal.from_dict(changed), "propose")
    with pytest.raises(ConflictError):
        repo.append("demo", SCOPE, proposal, "another-op")
    with pytest.raises(ConflictError):
        repo.append("demo", PrincipalScope("another", frozenset({"demo"})), proposal, "propose")
    assert repo.get("demo", SCOPE, "proposal", proposal.record_id) == proposal


def test_compare_and_swap_and_privileged_state_rejection(repo):
    proposal = add_proposal(repo)
    assert repo.transition_proposal("demo", SCOPE, proposal.record_id, 0, "PENDING_APPROVAL", "transition")
    assert not repo.transition_proposal("demo", SCOPE, proposal.record_id, 0, "PENDING_APPROVAL", "transition")
    with pytest.raises(ConflictError):
        repo.transition_proposal("demo", SCOPE, proposal.record_id, 0, "REJECTED", "stale")
    with pytest.raises(ContractError):
        repo.transition_proposal("demo", SCOPE, proposal.record_id, 1, "ACCEPTED", "privileged")
    assert repo.proposal_head("demo", SCOPE, proposal.record_id) == (1, "PENDING_APPROVAL")


@pytest.mark.parametrize("state", ["CONFLICTED", "STALE", "PENDING_APPROVAL", "REJECTED"])
def test_initial_proposal_cannot_skip_lifecycle(repo, state):
    add_evidence(repo)
    value = sample("proposal.valid.json")
    value["state"] = state
    with pytest.raises(ContractError):
        repo.append("demo", SCOPE, Proposal.from_dict(value), "skip-" + state)


def test_independent_connections_cannot_both_win_cas(repo):
    proposal = add_proposal(repo)
    barrier = Barrier(2)
    def writer(index):
        store = StateStore(repo.store.db_path)
        other = ContextRepository(store, repo.registry)
        try:
            barrier.wait(timeout=10)
            try:
                return other.transition_proposal("demo", SCOPE, proposal.record_id, 0, "REJECTED", f"writer-{index}")
            except ConflictError:
                return False
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(writer, range(2))) == [False, True]
    assert repo.proposal_head("demo", SCOPE, proposal.record_id) == (1, "REJECTED")


def test_atomic_rollback_on_event_failure(repo):
    add_evidence(repo)
    repo.store.conn.set_authorizer(
        lambda action, table, *args: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_INSERT and table == "v06_events" else sqlite3.SQLITE_OK
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            repo.append("demo", SCOPE, Proposal.from_dict(sample("proposal.valid.json")), "propose")
    finally:
        repo.store.conn.set_authorizer(None)
    assert repo.get("demo", SCOPE, "proposal", "proposal-demo-1") is None
    assert repo.proposal_head("demo", SCOPE, "proposal-demo-1") is None
    assert repo.store.conn.execute("SELECT COUNT(*) FROM v06_operations WHERE operation_id='propose'").fetchone()[0] == 0


def test_additive_migration_restart_backup_and_legacy_preservation(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = StateStore(path)
    task = Task(project_id="demo", goal_id="g", type="test", role="coder", definition_of_done=[])
    store.save_task(task)
    before = store.get_task(task.task_id)
    assert not store.conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'v06_%'").fetchall()
    backup = sqlite3.connect(tmp_path / "backup.sqlite3")
    store.conn.backup(backup)
    repo = ContextRepository(store, None)
    repo.initialize()
    repo.initialize()
    assert store.get_task(task.task_id) == before
    assert store.conn.execute("SELECT COUNT(*) FROM v06_migrations").fetchone()[0] == 1
    store.close()
    reopened = StateStore(path)
    assert reopened.get_task(task.task_id) == before
    assert not backup.execute("SELECT name FROM sqlite_master WHERE name LIKE 'v06_%'").fetchall()
    assert backup.execute("SELECT task_id FROM tasks").fetchone()[0] == task.task_id
    backup.close()
    reopened.close()


def test_future_migration_version_rejected(repo):
    repo.store.conn.execute("INSERT INTO v06_migrations VALUES (99, 'future')")
    repo.store.conn.commit()
    with pytest.raises(RuntimeError):
        repo.initialize()


def test_unmanaged_transaction_not_committed(repo):
    repo.store.conn.execute("BEGIN")
    with pytest.raises(RuntimeError):
        repo.initialize()
    assert repo.store.conn.in_transaction
    repo.store.conn.rollback()


@pytest.mark.parametrize("status", ["PASS", "FAIL", "UNKNOWN"])
def test_reports_bind_contract_and_evidence(repo, status):
    contract = OutcomeContract.from_dict(sample("contract.valid.json"))
    task = Task(project_id="demo", goal_id="g", type="test", role="coder", definition_of_done=[], task_id=contract.data["task_id"])
    repo.store.save_task(task)
    repo.append("demo", SCOPE, contract, "contract")
    report = VerificationReport.from_dict(sample(f"report.{status.lower()}.valid.json"))
    for check in report.data["checks"]:
        for eid in check["evidence_ids"]:
            add_evidence(repo, eid=eid, attempt=report.data["attempt_id"])
    assert repo.append("demo", SCOPE, report, "report")
    # Recording a PASS cannot change the task's existing status.
    assert repo.store.get_task(task.task_id)["status"] == task.status.value


@pytest.mark.parametrize("tamper", ["attempt", "project", "digest", "verifier", "status", "missing", "time"])
def test_report_forgery(tamper):
    contract = OutcomeContract.from_dict(sample("contract.valid.json"))
    report_data = sample("report.pass.valid.json")
    evidence_data = sample("evidence.valid.json")
    eid = report_data["checks"][0]["evidence_ids"][0]
    evidence_data.update(evidence_id=eid, attempt_id=report_data["attempt_id"])
    if tamper == "attempt": evidence_data["attempt_id"] = "other-attempt"
    elif tamper == "project": evidence_data["project_id"] = "other"
    elif tamper == "digest": report_data["contract_digest"] = "0" * 64
    elif tamper == "verifier": report_data["checks"][0]["verifier_version"] = "evil"
    elif tamper == "status": report_data["status"] = "UNKNOWN"
    elif tamper == "missing": report_data["checks"][0]["criterion_id"] = "different"
    else: evidence_data["observed_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(ContractError):
        VerificationReport.from_dict(report_data).validate_against(contract, {eid: EvidenceReference.from_dict(evidence_data)})


def test_revision_cannot_self_activate(repo):
    add_evidence(repo)
    value = sample("knowledge-revision.valid.json")
    repo.append("demo", SCOPE, KnowledgeRevision.from_dict(value), "stage")
    value.update(revision_id="revision-evil", state="ACCEPTED")
    with pytest.raises(ContractError):
        repo.append("demo", SCOPE, KnowledgeRevision.from_dict(value), "activate")


def test_memory_manifest_cannot_claim_unfinished_verification():
    value = sample("memory-migration.dry-run.valid.json")
    MemoryMigrationManifest.from_dict(value)
    value["status"] = "VERIFIED"
    with pytest.raises(ContractError):
        MemoryMigrationManifest.from_dict(value)


def test_memory_manifest_uses_future_global_store(repo):
    manifest = MemoryMigrationManifest.from_dict(sample("memory-migration.dry-run.valid.json"))
    with pytest.raises(ContractError, match="global record persistence is deferred"):
        repo.append("demo", SCOPE, manifest, "memory")
    with pytest.raises(ContractError, match="not project-scoped"):
        repo.get("demo", SCOPE, "memory-migration-manifest", manifest.record_id)


def test_tampered_stored_scope_is_rejected(repo):
    add_proposal(repo)
    value = sample("proposal.valid.json")
    value["project_id"] = "other"
    repo.store.conn.execute(
        "UPDATE v06_records SET payload=? WHERE project_id='demo' AND kind='proposal'",
        (canonical(value),),
    )
    repo.store.conn.commit()
    with pytest.raises(ContractError, match="stored record scope mismatch"):
        repo.get("demo", SCOPE, "proposal", value["proposal_id"])


def test_capture_reference_cannot_be_rebound(repo):
    add_evidence(repo)
    value = sample("intake.valid.json")
    value["content_sha256"] = digest(b"different")
    with pytest.raises(ConflictError):
        repo.append("demo", SCOPE, IntakeEnvelope.from_dict(value), "rebind", captured_bytes=b"different")


@pytest.mark.parametrize("kind,parameters", [
    ("artifact", {"artifact_rule_id": "rule-1"}),
    ("structured_data", {"schema_registry_id": "schema-1"}),
    ("code_test", {"test_profile_id": "tests.unit"}),
])
def test_typed_verifier_parameters(kind, parameters):
    assert parameters_for(kind, parameters)
    with pytest.raises(ContractError):
        parameters_for(kind, {**parameters, "command": "untrusted"})


@pytest.mark.parametrize("kind,parameters", [
    ("artifact", {"artifact_rule_id": "../../secret"}),
    ("artifact", {"artifact_rule_id": "rule", "max_bytes": True}),
    ("structured_data", {"schema_registry_id": "https://evil/schema"}),
    ("code_test", {"test_profile_id": "tests", "minimum_tests": 0}),
    ("research", {}),
])
def test_verifier_parameter_boundary(kind, parameters):
    with pytest.raises(ContractError):
        parameters_for(kind, parameters)


@pytest.mark.parametrize("declaration", [
    "project_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL",
    "project_id TEXT, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project_id,kind,record_id)",
    "project_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(project_id,kind,record_id)",
    "project_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(record_id)",
    "project_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project_id,kind,record_id) ON CONFLICT REPLACE",
    "project_id TEXT NOT NULL COLLATE NOCASE, kind TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(project_id,kind,record_id)",
], ids=["missing-pk", "nullable-project", "wrong-type", "wrong-pk", "replace-conflict", "collation"])
def test_incompatible_existing_schema_rejected(tmp_path, declaration):
    store = StateStore(tmp_path / "incompatible.sqlite3")
    try:
        store.conn.execute("CREATE TABLE v06_records (" + declaration + ")")
        store.conn.commit()
        with pytest.raises(RuntimeError, match="layout"):
            ContextRepository(store, None).initialize()
        assert store.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='v06_migrations'"
        ).fetchone() is None
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["append", "get", "head", "transition"])
@pytest.mark.parametrize("versions", [(), (1, 99)])
def test_every_operation_checks_ledger(repo, operation, versions):
    proposal = add_proposal(repo)
    repo.store.conn.execute("DELETE FROM v06_migrations")
    repo.store.conn.executemany("INSERT INTO v06_migrations VALUES (?, 'fixture')", [(v,) for v in versions])
    repo.store.conn.commit()
    with pytest.raises(RuntimeError, match="storage version"):
        if operation == "append":
            repo.append("demo", SCOPE, proposal, "propose")
        elif operation == "get":
            repo.get("demo", SCOPE, "proposal", proposal.record_id)
        elif operation == "head":
            repo.proposal_head("demo", SCOPE, proposal.record_id)
        else:
            repo.transition_proposal("demo", SCOPE, proposal.record_id, 0, "REJECTED", "transition")
    assert not repo.store.conn.in_transaction
    assert repo.store.conn.execute("SELECT COUNT(*) FROM v06_operations").fetchone()[0] == 2


def test_existing_version_cannot_silently_recreate_lost_tables(repo):
    repo.store.conn.execute("DROP TABLE v06_records")
    repo.store.conn.commit()
    for operation in (repo.initialize, lambda: repo.get("demo", SCOPE, "proposal", "p")):
        with pytest.raises(RuntimeError, match="layout"):
            operation()
    assert repo.store.conn.execute("SELECT name FROM sqlite_master WHERE name='v06_records'").fetchone() is None


def test_schema_cache_invalidated_after_ddl(repo):
    add_proposal(repo)
    repo.store.conn.execute("ALTER TABLE v06_records RENAME TO v06_records_old")
    repo.store.conn.execute("CREATE TABLE v06_records AS SELECT * FROM v06_records_old")
    repo.store.conn.commit()
    with pytest.raises(RuntimeError, match="layout"):
        repo.get("demo", SCOPE, "proposal", "proposal-demo-1")


def test_unexpected_trigger_rejected(repo):
    repo.store.conn.execute("CREATE TRIGGER ignore_record BEFORE INSERT ON v06_records BEGIN SELECT RAISE(IGNORE); END")
    repo.store.conn.commit()
    with pytest.raises(RuntimeError, match="trigger"):
        add_evidence(repo)


def test_stored_key_must_match_internal_id(repo):
    evidence = add_evidence(repo)
    value = evidence.data
    value["evidence_id"] = "different-id"
    repo.store.conn.execute("UPDATE v06_records SET payload=? WHERE kind='evidence-reference'", (canonical(value),))
    repo.store.conn.commit()
    with pytest.raises(ContractError, match="identity"):
        repo.get("demo", SCOPE, "evidence-reference", evidence.record_id)
    with pytest.raises(ContractError, match="identity"):
        repo.append("demo", SCOPE, Proposal.from_dict(sample("proposal.valid.json")), "proposal")


def test_evidence_mapping_key_must_match_internal_id():
    contract = OutcomeContract.from_dict(sample("contract.valid.json"))
    report = VerificationReport.from_dict(sample("report.pass.valid.json"))
    value = sample("evidence.valid.json")
    value.update(evidence_id="different-id", attempt_id=report.data["attempt_id"])
    eid = report.data["checks"][0]["evidence_ids"][0]
    with pytest.raises(ContractError):
        report.validate_against(contract, {eid: EvidenceReference.from_dict(value)})


@pytest.mark.parametrize("field,value", [
    ("evidence_id", "bad\x7f"), ("derived_from", ["bad\x7f"]),
    ("derived_from", ["x" * 129]), ("derived_from", [" "]),
])
def test_record_ids_and_references_use_api_identifier_rules(field, value):
    data = sample("evidence.valid.json")
    data[field] = value
    with pytest.raises(ContractError):
        EvidenceReference.from_dict(data)


@pytest.mark.parametrize("kind,filename", [
    ("intake", "intake.valid.json"), ("evidence-reference", "evidence.valid.json"),
    ("proposal", "proposal.valid.json"), ("knowledge-revision", "knowledge-revision.valid.json"),
    ("outcome-contract", "contract.valid.json"), ("verification-report", "report.pass.valid.json"),
    ("memory-migration-manifest", "memory-migration.dry-run.valid.json"),
])
def test_record_type_metadata_cannot_be_shadowed(kind, filename):
    record = RECORD_TYPES[kind].from_dict(sample(filename))
    for name, value in (("kind", "evidence-reference"), ("id_field", "project_id"), ("raw", "{}")):
        with pytest.raises(AttributeError):
            setattr(record, name, value)
