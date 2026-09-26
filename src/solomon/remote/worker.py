"""Octavryn Worker state and durable checkpoints (remote spec 16).

The Worker is the only thing that executes remote work, on the user's
own machine. It uses the same governance-gated execution path as the CLI
(execution.execute_with_fallback). This module holds the state machine,
the heartbeat and the durable checkpoint/queue records. The bridge
decides *whether* something may run; the Worker records *what actually
happened*, and never claims more than that:
planned != queued != running != completed != verified.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..state import StateStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_workers (
    worker_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    last_heartbeat TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS remote_requests (
    request_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    identity_ref TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_id TEXT,
    approval_id TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    envelope TEXT NOT NULL,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def ensure_schema(store: StateStore) -> None:
    with store._lock:
        store.conn.executescript(_SCHEMA)
        store.conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerState(str, Enum):
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    READY = "READY"
    BUSY = "BUSY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    BLOCKED = "BLOCKED"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    ERROR = "ERROR"


_ALLOWED = {
    WorkerState.OFFLINE: {WorkerState.STARTING},
    WorkerState.STARTING: {WorkerState.READY, WorkerState.DEGRADED, WorkerState.ERROR, WorkerState.OFFLINE},
    WorkerState.READY: {WorkerState.BUSY, WorkerState.DRAINING, WorkerState.DEGRADED, WorkerState.OFFLINE,
                        WorkerState.ERROR, WorkerState.WAITING_APPROVAL, WorkerState.BLOCKED},
    WorkerState.BUSY: {WorkerState.READY, WorkerState.WAITING_APPROVAL, WorkerState.BLOCKED,
                       WorkerState.DEGRADED, WorkerState.ERROR, WorkerState.DRAINING},
    WorkerState.WAITING_APPROVAL: {WorkerState.READY, WorkerState.BUSY, WorkerState.BLOCKED, WorkerState.OFFLINE},
    WorkerState.BLOCKED: {WorkerState.READY, WorkerState.OFFLINE},
    WorkerState.DEGRADED: {WorkerState.READY, WorkerState.OFFLINE, WorkerState.ERROR, WorkerState.DRAINING},
    WorkerState.DRAINING: {WorkerState.OFFLINE},
    WorkerState.ERROR: {WorkerState.OFFLINE, WorkerState.STARTING},
}


class InvalidTransition(RuntimeError):
    pass


@dataclass
class Worker:
    store: StateStore
    worker_id: str = "local-worker"
    heartbeat_timeout: timedelta = timedelta(seconds=90)

    def __post_init__(self):
        ensure_schema(self.store)
        with self.store._lock:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO remote_workers (worker_id, state) VALUES (?, ?)",
                (self.worker_id, WorkerState.OFFLINE.value),
            )
            self.store.conn.commit()

    @property
    def state(self) -> WorkerState:
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT state FROM remote_workers WHERE worker_id = ?", (self.worker_id,)).fetchone()
        return WorkerState(row[0])

    def transition(self, new: WorkerState, detail: str = "") -> None:
        cur = self.state
        if new != cur and new not in _ALLOWED[cur]:
            raise InvalidTransition(f"{cur.value} -> {new.value} not allowed")
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE remote_workers SET state = ?, detail = ? WHERE worker_id = ?",
                (new.value, detail, self.worker_id))
            self.store.conn.commit()

    def heartbeat(self, now: datetime | None = None) -> None:
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE remote_workers SET last_heartbeat = ? WHERE worker_id = ?",
                ((now or _now()).isoformat(), self.worker_id))
            self.store.conn.commit()

    def start(self, now: datetime | None = None) -> None:
        if self.state in (WorkerState.OFFLINE, WorkerState.ERROR):
            self.transition(WorkerState.STARTING)
        self.heartbeat(now)
        self.transition(WorkerState.READY)

    def stop(self) -> None:
        if self.state not in (WorkerState.OFFLINE, WorkerState.DRAINING):
            if WorkerState.DRAINING in _ALLOWED[self.state]:
                self.transition(WorkerState.DRAINING)
        if self.state != WorkerState.OFFLINE:
            self.transition(WorkerState.OFFLINE)

    def is_online(self, now: datetime | None = None) -> bool:
        """Online = state accepts work AND the heartbeat is fresh. A READY
        row with a stale heartbeat is treated as offline (the process may
        have died without updating its state)."""
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT state, last_heartbeat FROM remote_workers WHERE worker_id = ?",
                (self.worker_id,)).fetchone()
        state, hb = WorkerState(row[0]), row[1]
        if state not in (WorkerState.READY, WorkerState.BUSY, WorkerState.DEGRADED) or hb is None:
            return False
        return (now or _now()) - datetime.fromisoformat(hb) <= self.heartbeat_timeout


# -- durable request/checkpoint records --------------------------------------

def record_request(store: StateStore, *, request_id: str, correlation_id: str, identity_ref: str,
                   project_id: str, status: str, reason: str, envelope: dict,
                   task_id: str | None = None, approval_id: str | None = None) -> None:
    now = _now().isoformat()
    with store._lock:
        store.conn.execute(
            "INSERT INTO remote_requests (request_id, correlation_id, identity_ref, project_id, task_id, "
            "approval_id, status, reason, envelope, received_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, correlation_id, identity_ref, project_id, task_id, approval_id, status, reason,
             json.dumps(envelope, ensure_ascii=False), now, now))
        store.conn.commit()


def update_request(store: StateStore, request_id: str, status: str, reason: str = "") -> None:
    with store._lock:
        store.conn.execute(
            "UPDATE remote_requests SET status = ?, reason = ?, updated_at = ? WHERE request_id = ?",
            (status, reason, _now().isoformat(), request_id))
        store.conn.commit()


def get_request(store: StateStore, request_id: str) -> dict | None:
    with store._lock:
        row = store.conn.execute(
            "SELECT request_id, correlation_id, identity_ref, project_id, task_id, approval_id, status, "
            "reason, envelope, received_at, updated_at FROM remote_requests WHERE request_id = ?",
            (request_id,)).fetchone()
    if row is None:
        return None
    keys = ["request_id", "correlation_id", "identity_ref", "project_id", "task_id", "approval_id",
            "status", "reason", "envelope", "received_at", "updated_at"]
    d = dict(zip(keys, row))
    d["envelope"] = json.loads(d["envelope"])
    return d


def list_requests(store: StateStore, status: str | None = None, identity_ref: str | None = None) -> list[dict]:
    q = "SELECT request_id FROM remote_requests WHERE 1=1"
    params: list = []
    if status:
        q += " AND status = ?"
        params.append(status)
    if identity_ref:
        q += " AND identity_ref = ?"
        params.append(identity_ref)
    with store._lock:
        ids = [r[0] for r in store.conn.execute(q + " ORDER BY received_at", params).fetchall()]
    return [get_request(store, i) for i in ids]


def seen_request(store: StateStore, request_id: str) -> bool:
    with store._lock:
        return store.conn.execute(
            "SELECT 1 FROM remote_requests WHERE request_id = ?", (request_id,)).fetchone() is not None


def get_control(store: StateStore, key: str) -> str | None:
    with store._lock:
        row = store.conn.execute("SELECT value FROM remote_controls WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_control(store: StateStore, key: str, value: str, reason: str, changed_by: str) -> None:
    with store._lock:
        store.conn.execute(
            "INSERT OR REPLACE INTO remote_controls (key, value, reason, changed_by, changed_at) VALUES (?,?,?,?,?)",
            (key, value, reason, changed_by, _now().isoformat()))
        store.conn.commit()
    store.log_event(project_id="-", event="remote_control_changed", detail=f"{key}={value} by {changed_by}: {reason}")


def save_checkpoint(store: StateStore, request_id: str, task_id: str | None, status: str, data: dict) -> str:
    checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
    with store._lock:
        store.conn.execute(
            "INSERT INTO remote_checkpoints (checkpoint_id, request_id, task_id, status, data, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (checkpoint_id, request_id, task_id, status, json.dumps(data, ensure_ascii=False), _now().isoformat()))
        store.conn.commit()
    return checkpoint_id


def checkpoints_for(store: StateStore, request_id: str) -> list[dict]:
    with store._lock:
        rows = store.conn.execute(
            "SELECT checkpoint_id, task_id, status, data, created_at FROM remote_checkpoints "
            "WHERE request_id = ? ORDER BY created_at, rowid", (request_id,)).fetchall()
    return [{"checkpoint_id": r[0], "task_id": r[1], "status": r[2], "data": json.loads(r[3]),
             "created_at": r[4]} for r in rows]
