"""Explicit opt-in storage in StateStore's database; trusted core callers only.

No adapter, CLI or MCP path exposes this API. Scope is injected by the trusted
caller, never parsed from intake. Stored reports/revisions are evidence, not
active truth; activation and approval consumption belong to Phase 2.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from .contracts import (ContractError, Record, RECORD_TYPES,
                        VerificationReport, canonical, digest, identifier as _identifier)

PROJECT_RECORD_TYPES = frozenset(RECORD_TYPES) - {"memory-migration-manifest"}


class ConflictError(ValueError):
    pass


@dataclass(frozen=True)
class PrincipalScope:
    principal_id: str
    project_ids: frozenset[str]

    def __post_init__(self):
        _identifier(self.principal_id, "principal")
        if isinstance(self.project_ids, (str, bytes)):
            raise ValueError("project IDs must be a collection")
        projects = frozenset(self.project_ids)
        if len(projects) > 1000:
            raise ValueError("too many project scopes")
        for project_id in projects:
            _identifier(project_id, "project")
        object.__setattr__(self, "project_ids", projects)


DDL = (
    "CREATE TABLE IF NOT EXISTS v06_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS v06_records (
        project_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(project_id, kind, record_id))""",
    """CREATE TABLE IF NOT EXISTS v06_captures (
        project_id TEXT NOT NULL, content_ref TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        PRIMARY KEY(project_id, content_ref))""",
    """CREATE TABLE IF NOT EXISTS v06_operations (
        project_id TEXT NOT NULL, operation_id TEXT NOT NULL, binding TEXT NOT NULL,
        principal_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(project_id, operation_id))""",
    """CREATE TABLE IF NOT EXISTS v06_proposal_heads (
        project_id TEXT NOT NULL, proposal_id TEXT NOT NULL, version INTEGER NOT NULL,
        state TEXT NOT NULL, PRIMARY KEY(project_id, proposal_id))""",
    """CREATE TABLE IF NOT EXISTS v06_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
        operation_id TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL,
        version INTEGER, state TEXT)""",
)

TABLES = ("v06_migrations", "v06_records", "v06_captures", "v06_operations",
          "v06_proposal_heads", "v06_events")


@lru_cache(maxsize=1)
def _expected_layout():
    # Derive the expected columns and primary-key order from the actual DDL.
    # Only fixed internal table names are interpolated into PRAGMA statements.
    conn = sqlite3.connect(":memory:")
    try:
        for statement in DDL:
            conn.execute(statement)
        return {name: (
            tuple(conn.execute(f"PRAGMA table_info({name})")),
            " ".join(conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0].split()),
        ) for name in TABLES}
    finally:
        conn.close()


class ContextRepository:
    def __init__(self, state_store, project_registry):
        self.store = state_store
        self.registry = project_registry
        self._validated_schema_version = None

    def _check_layout(self, conn, *, allow_missing=False):
        for name, expected in _expected_layout().items():
            row = conn.execute("SELECT type, sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None and allow_missing:
                continue
            if (row is None or row[0] != "table" or
                    (tuple(conn.execute(f"PRAGMA table_info({name})")), " ".join(row[1].split())) != expected):
                raise RuntimeError("incompatible context storage layout: " + name)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (name,)).fetchone():
                raise RuntimeError("unexpected context storage trigger: " + name)

    def _check_ready(self, conn):
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        if self._validated_schema_version != schema_version:
            self._check_layout(conn)
            self._validated_schema_version = schema_version
        versions = {row[0] for row in conn.execute("SELECT version FROM v06_migrations")}
        if versions != {1}:
            raise RuntimeError("unsupported or uninitialized context storage version")

    @contextmanager
    def _transaction(self, *, write=True, check_ready=True):
        with self.store._lock:
            conn = self.store.conn
            if conn.in_transaction:
                raise RuntimeError("context operation cannot join an unmanaged transaction")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                if check_ready:
                    self._check_ready(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self) -> None:
        """Explicit additive migration. Caller backs up existing DB before opt-in.

        Atomic DDL and ledger are restart-safe; initialization never runs from
        StateStore.__init__ and never touches legacy rows or approvals.
        """
        with self._transaction(check_ready=False) as conn:
            self._check_layout(conn, allow_missing=True)
            ledger_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='v06_migrations'"
            ).fetchone()
            if ledger_exists:
                versions = {row[0] for row in conn.execute("SELECT version FROM v06_migrations")}
                if versions - {1}:
                    raise RuntimeError("unsupported context storage version")
                if versions:
                    self._check_layout(conn)
            for statement in DDL:
                conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO v06_migrations VALUES (1, ?)", (self._now(),))
            self._check_ready(conn)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _scope(self, project_id: str, scope: PrincipalScope, *, write=False):
        _identifier(project_id, "project")
        project = self.registry.get(project_id) if self.registry is not None else None
        if (not isinstance(scope, PrincipalScope) or
                project_id not in scope.project_ids or project is None):
            raise PermissionError("project outside authorized registered scope")
        if write and project.status != "active":
            raise PermissionError("project is not active for writes")

    def _get(self, conn, project, kind, rid):
        row = conn.execute("SELECT payload FROM v06_records WHERE project_id=? AND kind=? AND record_id=?",
                           (project, kind, rid)).fetchone()
        if row is None:
            return None
        record = RECORD_TYPES[kind](row[0])
        if record.project_id != project:
            raise ContractError("stored record scope mismatch")
        if record.record_id != rid:
            raise ContractError("stored record identity mismatch")
        return record

    def get(self, project_id: str, principal_scope: PrincipalScope, kind: str, record_id: str):
        self._scope(project_id, principal_scope)
        if kind not in PROJECT_RECORD_TYPES:
            raise ContractError("record kind is not project-scoped in Phase 1A")
        _identifier(record_id, "record")
        with self._transaction(write=False) as conn:
            return self._get(conn, project_id, kind, record_id)

    def _require(self, conn, project, kind, rid):
        result = self._get(conn, project, kind, rid)
        if result is None:
            raise ContractError("missing reference within project")
        return result

    def _references(self, conn, record):
        value, project = record.data, record.project_id
        for eid in value.get("evidence_ids", []):
            self._require(conn, project, "evidence-reference", eid)
        for eid in value.get("derived_from", []):
            self._require(conn, project, "evidence-reference", eid)
        predecessor = value.get("predecessor_revision_id")
        if predecessor:
            previous = self._require(conn, project, "knowledge-revision", predecessor)
            if previous.data["entry_id"] != value["entry_id"]:
                raise ContractError("predecessor belongs to another entry")
        if value.get("expected_revision"):
            self._require(conn, project, "knowledge-revision", value["expected_revision"])
        if record.kind in ("outcome-contract", "verification-report"):
            row = conn.execute("SELECT project_id FROM tasks WHERE task_id=?", (value["task_id"],)).fetchone()
            if row is None or row[0] != project:
                raise ContractError("task not found in project")
        if isinstance(record, VerificationReport):
            contract = self._require(conn, project, "outcome-contract", value["contract_id"])
            evidence = {eid: self._require(conn, project, "evidence-reference", eid)
                        for check in value["checks"] for eid in check["evidence_ids"]}
            record.validate_against(contract, evidence)

    def _operation(self, conn, project, scope, operation_id, binding):
        _identifier(operation_id, "operation")
        row = conn.execute("SELECT binding, principal_id FROM v06_operations WHERE project_id=? AND operation_id=?",
                           (project, operation_id)).fetchone()
        if row:
            if row != (binding, scope.principal_id):
                raise ConflictError("operation ID replay with different payload or principal")
            return False
        conn.execute("INSERT INTO v06_operations VALUES (?, ?, ?, ?, ?)",
                     (project, operation_id, binding, scope.principal_id, self._now()))
        return True

    def append(self, project_id: str, principal_scope: PrincipalScope, record: Record,
               operation_id: str, *, captured_bytes: bytes | None = None) -> bool:
        """Append one validated core record atomically with a metadata-only event.

        Return False for exact operation replay. Not a capture API: source
        storage/ACL and authenticity attribution remain the core caller's job.
        """
        self._scope(project_id, principal_scope, write=True)
        if type(record) not in RECORD_TYPES.values():
            raise ContractError("unsupported record type")
        if record.kind not in PROJECT_RECORD_TYPES:
            raise ContractError("global record persistence is deferred")
        record = type(record)(record.raw)  # Revalidate at the persistence boundary.
        if record.project_id != project_id:
            raise PermissionError("cross-project record")
        value = record.data
        if record.kind == "knowledge-revision" and value["state"] != "STAGED":
            raise ContractError("activation is not available in Phase 1A")
        if record.kind == "proposal" and value["state"] not in {"PROPOSED", "QUARANTINED"}:
            raise ContractError("initial proposal state must enter through the audited lifecycle")
        if record.kind in ("intake", "evidence-reference"):
            if (not isinstance(captured_bytes, bytes) or
                    len(captured_bytes) > 1048576 or digest(captured_bytes) != value["content_sha256"]):
                raise ContractError("exact bounded capture bytes required")
        binding = digest(canonical({"kind": record.kind, "record": value}).encode("utf-8"))
        with self._transaction() as conn:
            if not self._operation(conn, project_id, principal_scope, operation_id, binding):
                return False
            self._references(conn, record)
            if record.kind in ("intake", "evidence-reference"):
                capture = conn.execute("SELECT content_sha256 FROM v06_captures WHERE project_id=? AND content_ref=?",
                                       (project_id, value["content_ref"])).fetchone()
                if capture and capture[0] != value["content_sha256"]:
                    raise ConflictError("immutable capture reference rebound")
                conn.execute("INSERT OR IGNORE INTO v06_captures VALUES (?, ?, ?)",
                             (project_id, value["content_ref"], value["content_sha256"]))
            try:
                conn.execute("INSERT OR ABORT INTO v06_records VALUES (?, ?, ?, ?)",
                             (project_id, record.kind, record.record_id, record.raw))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("immutable record ID already exists") from exc
            if record.kind == "proposal":
                conn.execute("INSERT INTO v06_proposal_heads VALUES (?, ?, 0, ?)",
                             (project_id, record.record_id, value["state"]))
            conn.execute("INSERT INTO v06_events(project_id, operation_id, kind, record_id) VALUES (?, ?, ?, ?)",
                         (project_id, operation_id, record.kind, record.record_id))
        return True

    def proposal_head(self, project_id: str, principal_scope: PrincipalScope, proposal_id: str):
        self._scope(project_id, principal_scope)
        _identifier(proposal_id, "proposal")
        with self._transaction(write=False) as conn:
            return conn.execute("SELECT version, state FROM v06_proposal_heads WHERE project_id=? AND proposal_id=?",
                                (project_id, proposal_id)).fetchone()

    def transition_proposal(self, project_id: str, principal_scope: PrincipalScope,
                            proposal_id: str, expected_version: int, new_state: str,
                            operation_id: str) -> bool:
        """Pre-authorization lifecycle only. Database CAS works across processes."""
        self._scope(project_id, principal_scope, write=True)
        _identifier(proposal_id, "proposal")
        if type(expected_version) is not int or not 0 <= expected_version <= 9223372036854775806:
            raise ContractError("nonnegative version required")
        allowed = {
            "QUARANTINED": {"PROPOSED", "REJECTED"},
            "PROPOSED": {"CONFLICTED", "STALE", "PENDING_APPROVAL", "REJECTED"},
            "PENDING_APPROVAL": {"STALE", "CONFLICTED", "REJECTED"},
            "CONFLICTED": {"REJECTED"}, "STALE": {"REJECTED"}, "REJECTED": set(),
        }
        binding = digest(canonical({"proposal_id": proposal_id, "version": expected_version,
                                    "state": new_state}).encode("utf-8"))
        with self._transaction() as conn:
            if not self._operation(conn, project_id, principal_scope, operation_id, binding):
                return False
            row = conn.execute("SELECT version, state FROM v06_proposal_heads WHERE project_id=? AND proposal_id=?",
                               (project_id, proposal_id)).fetchone()
            if row is None or row[0] != expected_version:
                raise ConflictError("stale proposal version")
            if new_state not in allowed[row[1]]:
                raise ContractError("invalid or privileged transition")
            changed = conn.execute("UPDATE v06_proposal_heads SET version=version+1, state=? WHERE project_id=? AND proposal_id=? AND version=?",
                                   (new_state, project_id, proposal_id, expected_version))
            if changed.rowcount != 1:
                raise ConflictError("concurrent proposal update")
            conn.execute("INSERT INTO v06_events(project_id, operation_id, kind, record_id, version, state) VALUES (?, ?, 'proposal_transition', ?, ?, ?)",
                         (project_id, operation_id, proposal_id, expected_version + 1, new_state))
        return True
