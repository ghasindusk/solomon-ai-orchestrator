"""Token Budget (v0.4 Token & Compute Intelligence, Phase 6 reopen step 4/10
-- DECISIONS.md D27). Additional spec section 10 / Formal Spec v0.4 section
17.8: budgets at Global/Project/Agent/Model/Goal/Task scope, Token-counted
rather than currency.

This is deliberately parallel to, not a replacement for, usage.UsageManager's
existing USD-based check_budget()/pressure(): the Router still consumes the
USD-based signal today. Migrating the Router's usage_efficiency signal onto
this Token-based budget (and removing cost_usd as a Core routing input) is
step 7 -- see DECISIONS.md D27's 10-step plan. Budget exhaustion here must
never be used to skip required Safety/Verification (Formal Spec v0.4 section
17.8); enforcing that belongs to whichever policy path consumes
TokenBudgetStatus, not to this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .usage_record import UsageRecord

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "token_budget.example.yaml"
)

_SCOPE_FIELD = {
    "project": "project_id",
    "agent": "agent_id",
    "model": "model",
    "goal": "goal_id",
    "task": "task_id",
}


def _window_since(window: str) -> str | None:
    now = datetime.now(timezone.utc)
    if window == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if window == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return None  # "all_time" or unrecognized -- no cutoff


@dataclass
class TokenBudgetStatus:
    scope: str
    scope_id: str | None
    level: str  # "unknown" | "ok" | "warning" | "critical" | "hard_stop"
    pct_used: float | None
    limit_tokens: int | None
    total_tokens: int | None
    known_record_count: int
    unknown_record_count: int
    window: str = "all_time"
    actions: list[str] = field(default_factory=list)


class TokenBudgetManager:
    def __init__(self, config_path: Path | str | None = None):
        path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        with open(path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f) or {}
        self._global_budget = (self._config.get("budgets") or {}).get("global", {})
        self._scope_budgets = (self._config.get("budgets") or {}).get("scopes", {}) or {}
        self._actions = self._config.get("actions", {})

    def _budget_for(self, scope: str, scope_id: str | None) -> dict:
        budget = dict(self._global_budget)
        if scope != "global" and scope_id is not None:
            override = (self._scope_budgets.get(scope) or {}).get(scope_id)
            if override:
                budget.update(override)
        return budget

    def check(
        self,
        records: list[UsageRecord],
        scope: str = "global",
        scope_id: str | None = None,
    ) -> TokenBudgetStatus:
        if scope != "global" and scope not in _SCOPE_FIELD:
            raise ValueError(f"unknown token budget scope: {scope!r}, expected 'global' or one of {tuple(_SCOPE_FIELD)}")

        budget = self._budget_for(scope, scope_id)
        limit_tokens = budget.get("limit_tokens")
        window = budget.get("window", "all_time")
        since = _window_since(window)

        if scope != "global":
            field_name = _SCOPE_FIELD[scope]
            records = [r for r in records if getattr(r, field_name) == scope_id]

        total_tokens = 0
        known = 0
        unknown = 0
        for record in records:
            if since is not None and record.timestamp and record.timestamp < since:
                continue
            if record.tokens.total is None:
                unknown += 1
                continue
            total_tokens += record.tokens.total
            known += 1

        summed_total = total_tokens if known > 0 else None

        if limit_tokens is None or summed_total is None:
            return TokenBudgetStatus(
                scope=scope,
                scope_id=scope_id,
                level="unknown",
                pct_used=None,
                limit_tokens=limit_tokens,
                total_tokens=summed_total,
                known_record_count=known,
                unknown_record_count=unknown,
                window=window,
                actions=[],
            )

        pct_used = (summed_total / limit_tokens) * 100 if limit_tokens > 0 else 0.0
        warning_pct = budget.get("warning_percent", 70)
        critical_pct = budget.get("critical_percent", 90)
        hard_stop_pct = budget.get("hard_stop_percent", 100)

        if pct_used >= hard_stop_pct:
            level, actions = "hard_stop", self._actions.get("hard_stop", [])
        elif pct_used >= critical_pct:
            level, actions = "critical", self._actions.get("critical", [])
        elif pct_used >= warning_pct:
            level, actions = "warning", self._actions.get("warning", [])
        else:
            level, actions = "ok", []

        return TokenBudgetStatus(
            scope=scope,
            scope_id=scope_id,
            level=level,
            pct_used=pct_used,
            limit_tokens=limit_tokens,
            total_tokens=summed_total,
            known_record_count=known,
            unknown_record_count=unknown,
            window=window,
            actions=actions,
        )


def hard_stop_approval_reason(status: TokenBudgetStatus) -> str | None:
    """Phase 6 reopen step 10/10 (DECISIONS.md D27, D30): Formal Spec v0.4
    section 17.8 -- budget exhaustion must never be used to skip Safety/
    Verification; a hard_stop status routes to Human Approval instead of
    silently letting the task through or silently blocking it.

    Returns None (no approval needed on budget grounds) for every level
    except hard_stop, in which case it returns a human-readable reason
    string suitable for PolicyEngine's existing approval_request flow
    (see cli.py cmd_run_task, which calls this alongside the pre-existing
    risk-based requires_approval() check).
    """
    if status.level != "hard_stop":
        return None
    scope_desc = f"{status.scope} '{status.scope_id}'" if status.scope_id else status.scope
    return (
        f"Token Budget hard_stop for {scope_desc}: "
        f"{status.total_tokens}/{status.limit_tokens} tokens used ({status.pct_used:.0f}%)"
    )
