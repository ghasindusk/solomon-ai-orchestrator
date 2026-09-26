"""State Store (Architecture doc section 2 / FR-10 Logging).

SQLite-backed persistence for Goals, Tasks and the Task event/result log.
Markdown/JSON export is left to a later phase; this is the runtime source
of truth.

Thread-safe: a single StateStore instance may be shared across the Phase 3
parallel executor's worker threads. The connection is opened with
check_same_thread=False and every method that touches it holds an RLock
(reentrant so e.g. save_task's internal call to log_event doesn't
deadlock) -- external adapter.execute() calls (the actual slow, I/O-bound
work) still run concurrently; only the brief SQLite critical sections are
serialized.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Goal, Task, TaskStatus
from .result import TaskResult, Usage, UsageProvenance
from .usage_record import UsageRecord, usage_record_from_task_result

def _resolve_default_db_path() -> Path:
    # v0.5 R7: the migrated state/octavryn.sqlite3 once `octavryn migrate
    # state --apply` has run, else the legacy state/solomon.sqlite3
    # (OCTAVRYN_DB overrides both). See migration.py.
    from .migration import resolve_default_db_path

    return resolve_default_db_path()


_DEFAULT_DB_PATH = _resolve_default_db_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    data TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    project_id TEXT,
    goal_id TEXT,
    task_id TEXT,
    agent TEXT,
    event TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS file_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS worktrees (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    path TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    merged_at TEXT
);

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    risk TEXT NOT NULL,
    reason TEXT NOT NULL,
    task_params TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT
);

-- v0.5 R2: Intelligence Registry. Rows are never deleted -- a
-- participant that disappears becomes availability='absent', so
-- historical task_results/usage keyed by its id stay attributable.
CREATE TABLE IF NOT EXISTS intelligences (
    intelligence_id TEXT PRIMARY KEY,
    descriptor TEXT NOT NULL,
    availability TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

-- v0.5: evaluation records (spec 09 gate 9). Evidence only.
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    intelligence_id TEXT NOT NULL,
    skill_id TEXT,
    outcome TEXT NOT NULL,
    retries INTEGER NOT NULL,
    tokens INTEGER,
    latency_s REAL,
    participants TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

-- v0.5 R2: capability evidence a human explicitly confirmed. Evidence
-- only, never a permission grant.
CREATE TABLE IF NOT EXISTS capability_confirmations (
    intelligence_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (intelligence_id, capability)
);
"""


class StateStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self._add_column_if_missing("approval_requests", "decided_by", "TEXT")
            self.conn.commit()

    def _add_column_if_missing(self, table: str, column: str, decl: str) -> None:
        """Additive, idempotent in-place schema upgrade for databases
        created by an older version (v0.5 R6: approval_requests.decided_by).
        Existing rows get NULL, meaning 'not recorded', never a guess."""
        cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self):
        with self._lock:
            self.conn.close()

    def save_goal(self, goal: Goal) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO goals (goal_id, project_id, text, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (goal.goal_id, goal.project_id, goal.text, goal.status.value, goal.created_at),
            )
            self.conn.commit()
            self.log_event(project_id=goal.project_id, goal_id=goal.goal_id, event="goal_saved")

    def get_goal(self, goal_id: str) -> dict | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT goal_id, project_id, text, status, created_at FROM goals WHERE goal_id = ?",
                (goal_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["goal_id", "project_id", "text", "status", "created_at"]
        return dict(zip(keys, row))

    def save_task(self, task: Task) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO tasks (task_id, goal_id, project_id, data, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task.task_id,
                    task.goal_id,
                    task.project_id,
                    json.dumps(task.to_schema_dict(), ensure_ascii=False),
                    task.status.value,
                    task.created_at,
                ),
            )
            self.conn.commit()
            self.log_event(
                project_id=task.project_id,
                goal_id=task.goal_id,
                task_id=task.task_id,
                event="task_saved",
                detail=task.status.value,
            )

    def save_result(self, result: TaskResult) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO task_results (task_id, data, recorded_at) VALUES (?, ?, ?)",
                (
                    result.task_id,
                    json.dumps(result.to_dict(), ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self.conn.commit()
            self.log_event(
                task_id=result.task_id,
                agent=result.agent,
                event="task_result_recorded",
                detail=result.status,
            )

    def log_event(
        self,
        event: str,
        project_id: str | None = None,
        goal_id: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO event_log (ts, project_id, goal_id, task_id, agent, event, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    project_id,
                    goal_id,
                    task_id,
                    agent,
                    event,
                    detail,
                ),
            )
            self.conn.commit()

    def list_events(self, project_id: str | None = None) -> list[dict]:
        """FR-10/FR-17: the durable, human-exportable event log. The
        SQLite file itself is gitignored runtime state, not vault
        content -- `solomon.cli export-log` uses this to write a real
        Markdown record into the Obsidian vault instead."""
        with self._lock:
            query = (
                "SELECT id, ts, project_id, goal_id, task_id, agent, event, detail "
                "FROM event_log WHERE 1=1"
            )
            params: list[str] = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            query += " ORDER BY id"
            cur = self.conn.execute(query, params)
            keys = ["id", "ts", "project_id", "goal_id", "task_id", "agent", "event", "detail"]
            return [dict(zip(keys, row)) for row in cur.fetchall()]

    def list_events_v04(self, project_id: str | None = None) -> list[dict]:
        """Same rows as list_events, reshaped to match v0.4's
        07_Schemas/event.schema.json field names exactly (event_id,
        timestamp, event_type, agent_id, payload as an object) for
        portable JSON/JSONL export. Additive: list_events/the Markdown
        export path are untouched, this is a new export shape only."""
        return [
            {
                "event_id": f"evt-{row['id']}",
                "timestamp": row["ts"],
                "event_type": row["event"],
                "project_id": row["project_id"],
                "goal_id": row["goal_id"],
                "task_id": row["task_id"],
                "agent_id": row["agent"],
                "payload": {"detail": row["detail"]},
            }
            for row in self.list_events(project_id=project_id)
        ]

    def reconcile_stale_tasks(
        self, stale_after_minutes: int = 30, now: "datetime | None" = None
    ) -> list[dict]:
        """v0.4 Architecture 'Recovery': on restart, reconcile in-flight
        tasks against reality and mark them resumable/retryable/unknown/
        failed rather than leaving them silently stuck. Real-architecture
        note: execution.py never actually writes ASSIGNED/RUNNING today --
        a task goes QUEUED -> RESULT_RECEIVED/FAILED within one synchronous
        adapter call (there is no persistent daemon, D14) -- so QUEUED is
        the status that can actually go stale (process killed before the
        adapter call returned); ASSIGNED/RUNNING are included defensively
        for when a future phase starts using them. This marks UNKNOWN
        rather than guessing COMPLETE/FAILED -- a human decides whether to
        retry (honest data over fabricated outcomes, same principle as
        task_status_from_result_status)."""
        now = now or datetime.now(timezone.utc)
        in_flight_statuses = ["QUEUED", "ASSIGNED", "RUNNING"]
        reconciled: list[dict] = []
        with self._lock:
            for task_row in self.list_tasks(statuses=in_flight_statuses):
                task_id = task_row["task_id"]
                cur = self.conn.execute(
                    "SELECT MAX(ts) FROM event_log WHERE task_id = ?", (task_id,)
                )
                last_activity_str = cur.fetchone()[0]
                if last_activity_str is None:
                    continue
                last_activity = datetime.fromisoformat(last_activity_str)
                age_minutes = (now - last_activity).total_seconds() / 60
                if age_minutes < stale_after_minutes:
                    continue

                cur = self.conn.execute("SELECT data FROM tasks WHERE task_id = ?", (task_id,))
                data = json.loads(cur.fetchone()[0])
                previous_status = data.get("status")
                data["status"] = TaskStatus.UNKNOWN.value
                self.conn.execute(
                    "UPDATE tasks SET data = ?, status = ? WHERE task_id = ?",
                    (json.dumps(data, ensure_ascii=False), TaskStatus.UNKNOWN.value, task_id),
                )
                self.conn.commit()
                self.log_event(
                    project_id=task_row.get("project_id"),
                    task_id=task_id,
                    event="task_reconciled_unknown",
                    detail=f"stale {age_minutes:.0f}min in {previous_status}",
                )
                reconciled.append(data)
        return reconciled

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            cur = self.conn.execute("SELECT data FROM tasks WHERE task_id = ?", (task_id,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def get_task_results(self, task_id: str) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT data FROM task_results WHERE task_id = ? ORDER BY id", (task_id,)
            )
            return [json.loads(row[0]) for row in cur.fetchall()]

    def list_tasks(self, project_id: str | None = None, statuses: list[str] | None = None) -> list[dict]:
        """Task rows as their schema-shaped dict (see Task.to_schema_dict),
        newest first. Backing data for the Phase 6 dashboard's task queue
        and for the Phase 3 parallel executor's own bookkeeping."""
        with self._lock:
            query = "SELECT data FROM tasks WHERE 1=1"
            params: list[str] = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            query += " ORDER BY created_at DESC"
            cur = self.conn.execute(query, params)
            tasks = [json.loads(row[0]) for row in cur.fetchall()]
        if statuses is not None:
            tasks = [t for t in tasks if t.get("status") in statuses]
        return tasks

    def _iter_result_rows(self):
        """Shared cursor for every stats/usage aggregation method: each
        task_result joined with its originating task's project_id and
        full schema dict (for role/type). One JOIN query, several
        consumers -- avoids scanning task_results repeatedly per call."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT tr.data, t.project_id, t.data FROM task_results tr "
                "LEFT JOIN tasks t ON t.task_id = tr.task_id"
            )
            rows = cur.fetchall()
        for result_json, row_project_id, task_json in rows:
            result = json.loads(result_json)
            task = json.loads(task_json) if task_json else {}
            yield result, row_project_id, task

    def get_adapter_stats(
        self, agent: str, project_id: str | None = None, role: str | None = None
    ) -> dict:
        """Real (not fabricated) routing signal for the Router: recorded
        TaskResult history for `agent`, optionally scoped to a project
        and/or a Task role (Phase 7: role-scoped stats power the Learning
        Router's per-task-type calibration). Returns neutral-safe None
        values when there is no history yet -- callers must not treat
        that as measured data."""
        count = 0
        successes = 0
        durations: list[float] = []
        for result, row_project_id, task in self._iter_result_rows():
            if result.get("agent") != agent:
                continue
            if project_id is not None and row_project_id != project_id:
                continue
            if role is not None and task.get("role") != role:
                continue
            count += 1
            if result.get("status") == "RESULT_RECEIVED":
                successes += 1
            try:
                start = datetime.fromisoformat(result["started_at"])
                finish = datetime.fromisoformat(result["finished_at"])
                durations.append((finish - start).total_seconds())
            except (KeyError, ValueError, TypeError):
                pass
        return {
            "count": count,
            "success_rate": (successes / count) if count else None,
            "avg_duration_seconds": (sum(durations) / len(durations)) if durations else None,
        }

    def get_verified_completions(self, agent: str) -> dict[str, int]:
        """v0.5 R2: {role: count} of Tasks run by `agent` that reached
        COMPLETE, i.e. passed their Definition of Done. RESULT_RECEIVED
        alone does not count: an agent claiming success is not evidence
        (GLOBAL_POLICY completion.agent_claim_is_sufficient: false)."""
        counts: dict[str, int] = {}
        seen: set[str] = set()
        for result, _row_project_id, task in self._iter_result_rows():
            if result.get("agent") != agent or task.get("status") != "COMPLETE":
                continue
            task_id = task.get("task_id")
            if task_id in seen:
                continue
            seen.add(task_id)
            role = task.get("role") or "unknown"
            counts[role] = counts.get(role, 0) + 1
        return counts

    def save_evaluation(self, rec) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO evaluations (task_id, intelligence_id, skill_id, outcome, retries, tokens, latency_s, "
                "participants, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.task_id, rec.intelligence_id, rec.skill_id, rec.outcome, rec.retries, rec.tokens,
                 rec.latency_s, json.dumps(rec.participants), rec.recorded_at),
            )
            self.conn.commit()

    def list_evaluations(self, task_id: str | None = None) -> list[dict]:
        with self._lock:
            q = ("SELECT task_id, intelligence_id, skill_id, outcome, retries, tokens, latency_s, participants, "
                 "recorded_at FROM evaluations")
            params: tuple = ()
            if task_id:
                q += " WHERE task_id = ?"
                params = (task_id,)
            rows = self.conn.execute(q + " ORDER BY eval_id", params).fetchall()
        keys = ["task_id", "intelligence_id", "skill_id", "outcome", "retries", "tokens", "latency_s",
                "participants", "recorded_at"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["participants"] = json.loads(d["participants"])
            out.append(d)
        return out

    def upsert_intelligence(self, intelligence_id: str, descriptor: dict, availability: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT INTO intelligences (intelligence_id, descriptor, availability, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(intelligence_id) DO UPDATE SET "
                "descriptor = excluded.descriptor, availability = excluded.availability, "
                "last_seen = excluded.last_seen",
                (intelligence_id, json.dumps(descriptor, ensure_ascii=False), availability, now, now),
            )
            self.conn.commit()

    def mark_intelligence_absent(self, intelligence_id: str) -> None:
        """Keeps the row and descriptor (historical identity); only the
        availability changes. last_seen is left as the last real sighting."""
        with self._lock:
            self.conn.execute(
                "UPDATE intelligences SET availability = 'absent' WHERE intelligence_id = ?",
                (intelligence_id,),
            )
            self.conn.commit()

    def list_intelligences(self) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT intelligence_id, descriptor, availability, first_seen, last_seen "
                "FROM intelligences ORDER BY intelligence_id"
            )
            rows = cur.fetchall()
        return [
            {
                "intelligence_id": r[0],
                "descriptor": json.loads(r[1]),
                "availability": r[2],
                "first_seen": r[3],
                "last_seen": r[4],
            }
            for r in rows
        ]

    def confirm_capability(self, intelligence_id: str, capability: str, confirmed_by: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO capability_confirmations "
                "(intelligence_id, capability, confirmed_by, confirmed_at) VALUES (?, ?, ?, ?)",
                (intelligence_id, capability, confirmed_by, datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()

    def list_capability_confirmations(self, intelligence_id: str | None = None) -> list[dict]:
        with self._lock:
            query = "SELECT intelligence_id, capability, confirmed_by, confirmed_at FROM capability_confirmations"
            params: tuple = ()
            if intelligence_id is not None:
                query += " WHERE intelligence_id = ?"
                params = (intelligence_id,)
            rows = self.conn.execute(query, params).fetchall()
        return [
            {"intelligence_id": r[0], "capability": r[1], "confirmed_by": r[2], "confirmed_at": r[3]}
            for r in rows
        ]

    def get_role_performance(self, project_id: str | None = None) -> dict[str, dict[str, dict]]:
        """Phase 7 Learning Router: {role: {agent: {count, successes,
        success_rate}}} -- the raw project/task-type performance table a
        human (or the Router) can inspect to see exactly what data is
        driving routing decisions. No project_id scoping is also valid
        (global performance across all projects)."""
        table: dict[str, dict[str, dict]] = {}
        for result, row_project_id, task in self._iter_result_rows():
            if project_id is not None and row_project_id != project_id:
                continue
            role = task.get("role")
            agent = result.get("agent")
            if not role or not agent:
                continue
            bucket = table.setdefault(role, {}).setdefault(agent, {"count": 0, "successes": 0})
            bucket["count"] += 1
            if result.get("status") == "RESULT_RECEIVED":
                bucket["successes"] += 1
        for role, agents in table.items():
            for agent, bucket in agents.items():
                bucket["success_rate"] = bucket["successes"] / bucket["count"] if bucket["count"] else None
        return table

    def get_usage_summary(self, project_id: str | None = None, since: str | None = None) -> dict:
        """Real (not fabricated) usage aggregation for the Usage Manager
        (Phase 4): sums recorded Usage across task_results, optionally
        scoped to a project and/or a time window (`since`: ISO timestamp,
        inclusive -- entries whose `finished_at` predates it are excluded;
        entries with an unparseable/missing `finished_at` are still
        counted rather than silently dropped, since excluding them would
        understate usage more than including them overstates it). Phase 4
        gained daily/monthly budget windows this way rather than adding a
        second aggregation path. cost_usd is only summed from entries
        that actually reported one (claude_code's total_cost_usd today);
        other providers contribute token counts but no cost figure yet --
        the `cost_provenance` field says whether any real cost data fed
        the sum."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT tr.data, t.project_id FROM task_results tr "
                "LEFT JOIN tasks t ON t.task_id = tr.task_id"
            )
            rows = cur.fetchall()

        since_dt = datetime.fromisoformat(since) if since else None
        total_cost_usd = 0.0
        had_cost_data = False
        total_input_tokens = 0
        total_output_tokens = 0
        per_agent: dict[str, dict] = {}
        local_telemetry: list[dict] = []

        for data_json, row_project_id in rows:
            result = json.loads(data_json)
            if project_id is not None and row_project_id != project_id:
                continue
            if since_dt is not None:
                try:
                    if datetime.fromisoformat(result["finished_at"]) < since_dt:
                        continue
                except (KeyError, ValueError, TypeError):
                    pass  # no parseable timestamp -- count it rather than silently drop it
            usage = result.get("usage") or {}
            agent = result.get("agent", "unknown")

            cost = usage.get("cost_usd")
            if isinstance(cost, (int, float)):
                total_cost_usd += cost
                had_cost_data = True

            in_tok = usage.get("input_tokens") or 0
            out_tok = usage.get("output_tokens") or 0
            total_input_tokens += in_tok
            total_output_tokens += out_tok

            bucket = per_agent.setdefault(
                agent, {"count": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            bucket["count"] += 1
            bucket["input_tokens"] += in_tok
            bucket["output_tokens"] += out_tok
            if isinstance(cost, (int, float)):
                bucket["cost_usd"] += cost

            if agent == "localai_ollama" and usage.get("provenance") == "CLI_REPORTED":
                try:
                    duration = (
                        datetime.fromisoformat(result["finished_at"])
                        - datetime.fromisoformat(result["started_at"])
                    ).total_seconds()
                    if duration > 0 and out_tok:
                        local_telemetry.append(
                            {
                                "task_id": result.get("task_id"),
                                "generated_tokens": out_tok,
                                "inference_duration_seconds": duration,
                                "tokens_per_second": out_tok / duration,
                            }
                        )
                except (KeyError, ValueError, TypeError):
                    pass

        return {
            "project_id": project_id,
            "total_cost_usd": total_cost_usd if had_cost_data else None,
            "cost_provenance": "CLI_REPORTED" if had_cost_data else "UNKNOWN",
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "per_agent": per_agent,
            "local_telemetry": local_telemetry,
        }

    def get_usage_records(
        self, project_id: str | None = None, since: str | None = None
    ) -> list[UsageRecord]:
        """Normalized UsageRecords (v0.4 Token & Compute Intelligence,
        Phase 6 reopen step 4, DECISIONS.md D27) for every task_results row
        that still has its parent task on record. Same source table and
        since/project_id filtering as get_usage_summary (kept as-is for the
        USD-based UsageManager/Router path until step 7); this is the
        Token-based counterpart used by usage_aggregation/TokenBudgetManager.

        A task_results row whose parent task has since been deleted has
        nowhere to attribute project_id/goal_id to and is skipped -- this
        should not happen in practice (tasks are never deleted) but silently
        fabricating a project_id would be worse than skipping.
        """
        with self._lock:
            cur = self.conn.execute(
                "SELECT tr.data, t.data FROM task_results tr LEFT JOIN tasks t ON t.task_id = tr.task_id"
            )
            rows = cur.fetchall()

        since_dt = datetime.fromisoformat(since) if since else None
        records: list[UsageRecord] = []
        for result_json, task_json in rows:
            if task_json is None:
                continue
            result_data = json.loads(result_json)
            task_data = json.loads(task_json)

            if project_id is not None and task_data.get("project_id") != project_id:
                continue
            if since_dt is not None:
                try:
                    if datetime.fromisoformat(result_data["finished_at"]) < since_dt:
                        continue
                except (KeyError, ValueError, TypeError):
                    pass  # no parseable timestamp -- count it, matching get_usage_summary

            task = Task.from_schema_dict(task_data)
            usage_data = result_data.get("usage") or {}
            usage = Usage(
                input_tokens=usage_data.get("input_tokens"),
                output_tokens=usage_data.get("output_tokens"),
                context_tokens=usage_data.get("context_tokens"),
                cost_usd=usage_data.get("cost_usd"),
                provenance=usage_data.get("provenance", UsageProvenance.UNKNOWN),
            )
            result = TaskResult(
                task_id=result_data.get("task_id", task.task_id),
                status=result_data.get("status", "UNKNOWN"),
                summary=result_data.get("summary", ""),
                agent=result_data.get("agent", "unknown"),
                started_at=result_data.get("started_at") or result_data.get("finished_at", ""),
                finished_at=result_data.get("finished_at", ""),
                usage=usage,
            )
            records.append(usage_record_from_task_result(task, result))

        return records

    def acquire_locks(self, task_id: str, project_id: str, paths: list[str]) -> bool:
        """Advisory file lock (FR-15). Claims every path in `paths` for
        `task_id` only if none of them is already held (unreleased) by a
        different task. Returns False -- claiming nothing -- on any
        conflict, so the caller must not proceed with a conflicting write.
        The whole check-then-claim sequence runs under the RLock so two
        threads racing for the same path can't both win."""
        normalized = [str(Path(p).resolve()) for p in paths]
        if not normalized:
            return True
        with self._lock:
            placeholders = ",".join("?" * len(normalized))
            cur = self.conn.execute(
                f"SELECT DISTINCT path, task_id FROM file_locks "  # nosec B608 - only "?" placeholders interpolated
                f"WHERE released_at IS NULL AND path IN ({placeholders})",
                normalized,
            )
            conflicts = [(path, holder) for path, holder in cur.fetchall() if holder != task_id]
            if conflicts:
                return False

            now = datetime.now(timezone.utc).isoformat()
            for path in normalized:
                self.conn.execute(
                    "INSERT INTO file_locks (task_id, project_id, path, acquired_at) VALUES (?, ?, ?, ?)",
                    (task_id, project_id, path, now),
                )
            self.conn.commit()
            self.log_event(
                project_id=project_id, task_id=task_id, event="locks_acquired", detail=",".join(normalized)
            )
        return True

    def release_locks(self, task_id: str) -> None:
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                "UPDATE file_locks SET released_at = ? WHERE task_id = ? AND released_at IS NULL",
                (now, task_id),
            )
            self.conn.commit()
            self.log_event(task_id=task_id, event="locks_released")

    def create_worktree_record(
        self, task_id: str, project_id: str, path: str, branch: str, base_branch: str
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO worktrees (task_id, project_id, path, branch, base_branch, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (task_id, project_id, path, branch, base_branch, datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
            self.log_event(project_id=project_id, task_id=task_id, event="worktree_created", detail=branch)

    def get_worktree(self, task_id: str) -> dict | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT task_id, project_id, path, branch, base_branch, status, created_at, merged_at "
                "FROM worktrees WHERE task_id = ?",
                (task_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["task_id", "project_id", "path", "branch", "base_branch", "status", "created_at", "merged_at"]
        return dict(zip(keys, row))

    def list_worktrees(self, project_id: str | None = None, status: str | None = None) -> list[dict]:
        with self._lock:
            query = "SELECT task_id FROM worktrees WHERE 1=1"
            params: list[str] = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at"
            cur = self.conn.execute(query, params)
            ids = [row[0] for row in cur.fetchall()]
        return [self.get_worktree(i) for i in ids]

    def update_worktree_status(self, task_id: str, status: str) -> None:
        with self._lock:
            merged_at = datetime.now(timezone.utc).isoformat() if status == "merged" else None
            self.conn.execute(
                "UPDATE worktrees SET status = ?, merged_at = COALESCE(?, merged_at) WHERE task_id = ?",
                (status, merged_at, task_id),
            )
            self.conn.commit()
            self.log_event(task_id=task_id, event="worktree_status_changed", detail=status)

    def create_approval_request(
        self, request_id: str, project_id: str, risk: str, reason: str, task_params: dict
    ) -> None:
        """FR-16 Human Approval: persist a pending approval request. Solomon
        must not proceed with the gated action until a human decides it
        via decide_approval_request."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO approval_requests "
                "(request_id, project_id, risk, reason, task_params, status, requested_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (
                    request_id,
                    project_id,
                    risk,
                    reason,
                    json.dumps(task_params, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self.conn.commit()
            self.log_event(
                project_id=project_id, event="approval_requested", detail=f"{request_id}:{risk}"
            )

    def get_approval_request(self, request_id: str) -> dict | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT request_id, project_id, risk, reason, task_params, status, "
                "requested_at, decided_at, decision_note, decided_by FROM approval_requests WHERE request_id = ?",
                (request_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = [
            "request_id", "project_id", "risk", "reason", "task_params",
            "status", "requested_at", "decided_at", "decision_note", "decided_by",
        ]
        entry = dict(zip(keys, row))
        entry["task_params"] = json.loads(entry["task_params"])
        return entry

    def consume_approval_request(self, request_id: str) -> bool:
        """v0.5 R6: approved -> executed, exactly once. The conditional
        UPDATE is atomic under the lock, so two concurrent executions of
        one approval cannot both succeed."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE approval_requests SET status = 'executed' WHERE request_id = ? AND status = 'approved'",
                (request_id,),
            )
            self.conn.commit()
            ok = cur.rowcount == 1
        if ok:
            req = self.get_approval_request(request_id)
            self.log_event(project_id=req["project_id"], event="approval_consumed", detail=request_id)
        return ok

    def list_approval_requests(self, project_id: str | None = None, status: str | None = None) -> list[dict]:
        with self._lock:
            query = "SELECT request_id FROM approval_requests WHERE 1=1"
            params: list[str] = []
            if project_id is not None:
                query += " AND project_id = ?"
                params.append(project_id)
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY requested_at"
            cur = self.conn.execute(query, params)
            ids = [row[0] for row in cur.fetchall()]
            return [self.get_approval_request(i) for i in ids]

    def decide_approval_request(
        self, request_id: str, approved: bool, note: str = "", decided_by: str | None = None
    ) -> bool:
        """Returns False if the request doesn't exist or is no longer pending
        (decisions are not overwritten -- a decided request stays decided).
        decided_by (v0.5): the human identity reference that decided."""
        with self._lock:
            existing = self.get_approval_request(request_id)
            if existing is None or existing["status"] != "pending":
                return False
            self.conn.execute(
                "UPDATE approval_requests SET status = ?, decided_at = ?, decision_note = ?, decided_by = ? "
                "WHERE request_id = ?",
                (
                    "approved" if approved else "denied",
                    datetime.now(timezone.utc).isoformat(),
                    note,
                    decided_by,
                    request_id,
                ),
            )
            self.conn.commit()
            self.log_event(
                project_id=existing["project_id"],
                event="approval_decided",
                detail=f"{request_id}:{'approved' if approved else 'denied'}",
            )
            return True
