"""Goal/Task data model, aligned with 04_Config_Schemas/task.schema.json."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Risk(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"
    CRITICAL = "CRITICAL"


class CommunicationMode(str, Enum):
    DIRECT = "DIRECT"
    REVIEW = "REVIEW"
    DEBATE = "DEBATE"


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


def task_status_from_result_status(result_status: str) -> "TaskStatus":
    """Agent output is evidence, not completion (spec section 6) -- this
    only reflects execution outcome (did an adapter call produce a
    result or fail), never jumps to COMPLETE, since that requires a
    Definition-of-Done check this codebase doesn't implement yet."""
    return TaskStatus.RESULT_RECEIVED if result_status == "RESULT_RECEIVED" else TaskStatus.FAILED


class GoalStatus(str, Enum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class Goal:
    project_id: str
    text: str
    goal_id: str = field(default_factory=lambda: _new_id("goal"))
    status: GoalStatus = GoalStatus.CREATED
    created_at: str = field(default_factory=_utcnow)


@dataclass
class Task:
    goal_id: str
    project_id: str
    type: str
    role: str
    definition_of_done: list[str]
    task_id: str = field(default_factory=lambda: _new_id("task"))
    risk: Risk = Risk.NORMAL
    status: TaskStatus = TaskStatus.QUEUED
    dependencies: list[str] = field(default_factory=list)
    context_budget_tokens: int | None = None
    assigned_agent: str | None = None
    communication_mode: CommunicationMode = CommunicationMode.DIRECT
    autonomy: int | None = None
    created_at: str = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        # v0.4 task.schema.json: autonomy is an optional 0-5 dial (Phase 8
        # UX autonomy dial). Not yet wired to any behavior -- this is just
        # the data model catching up to the schema per Phase 1 migration.
        if self.autonomy is not None and not (0 <= self.autonomy <= 5):
            raise ValueError(f"autonomy must be 0-5 or None, got {self.autonomy!r}")

    def to_schema_dict(self) -> dict:
        """Serialize matching task.schema.json's shape exactly."""
        return {
            "task_id": self.task_id,
            "goal_id": self.goal_id,
            "project_id": self.project_id,
            "type": self.type,
            "role": self.role,
            "risk": self.risk.value,
            "status": self.status.value,
            "dependencies": self.dependencies,
            "context_budget_tokens": self.context_budget_tokens,
            "assigned_agent": self.assigned_agent,
            "communication_mode": self.communication_mode.value,
            "definition_of_done": self.definition_of_done,
            "autonomy": self.autonomy,
        }

    @classmethod
    def from_schema_dict(cls, data: dict) -> "Task":
        """Inverse of to_schema_dict -- reconstructs a Task from a
        StateStore.get_task/list_tasks row, e.g. for `verify-task` to
        re-check an already-recorded Task's Definition of Done."""
        return cls(
            task_id=data["task_id"],
            goal_id=data["goal_id"],
            project_id=data["project_id"],
            type=data["type"],
            role=data["role"],
            risk=Risk(data["risk"]),
            status=TaskStatus(data["status"]),
            dependencies=list(data.get("dependencies") or []),
            context_budget_tokens=data.get("context_budget_tokens"),
            assigned_agent=data.get("assigned_agent"),
            communication_mode=CommunicationMode(data.get("communication_mode", "DIRECT")),
            definition_of_done=list(data.get("definition_of_done") or []),
            autonomy=data.get("autonomy"),
        )
