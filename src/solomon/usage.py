"""Usage Manager -- lean Phase 4 slice (Architecture doc section 2 "Usage
Manager" / FR-11 Token/Usage Management / FR-12 Budget-Aware Routing).

Budgets support a `window` (all_time | daily | monthly, per
04_Config_Schemas/usage_budget.example.yaml) -- daily/monthly reset at
the UTC calendar boundary, computed on read (no scheduled job resets
anything; "today"/"this month" is evaluated fresh on every
check_budget call). Cost is only real where an adapter actually
reports one (claude_code's total_cost_usd today); everything else
contributes token counts but no dollar figure -- BudgetStatus.provenance
says which.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .state import StateStore

_DEFAULT_BUDGET_PATH = (
    Path(__file__).resolve().parents[2] / "04_Config_Schemas" / "usage_budget.example.yaml"
)


def _window_since(window: str) -> str | None:
    now = datetime.now(timezone.utc)
    if window == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if window == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return None  # "all_time" or unrecognized -- no cutoff, preserves prior behavior


@dataclass
class BudgetStatus:
    project_id: str | None
    level: str  # "unknown" | "ok" | "warning" | "critical" | "hard_stop"
    pct_used: float | None
    limit_usd: float | None
    total_cost_usd: float | None
    provenance: str
    window: str = "all_time"
    actions: list[str] = field(default_factory=list)


class UsageManager:
    def __init__(self, state: StateStore, budget_path: Path | str | None = None):
        self._state = state
        path = Path(budget_path) if budget_path else _DEFAULT_BUDGET_PATH
        with open(path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f) or {}
        self._global_budget = (self._config.get("budgets") or {}).get("global", {})
        self._project_budgets = (self._config.get("budgets") or {}).get("projects", {}) or {}
        self._actions = self._config.get("actions", {})

    def get_project_usage(self, project_id: str | None = None, since: str | None = None) -> dict:
        return self._state.get_usage_summary(project_id=project_id, since=since)

    def check_budget(self, project_id: str) -> BudgetStatus:
        budget = {**self._global_budget, **(self._project_budgets.get(project_id) or {})}
        limit_usd = budget.get("limit_usd")
        window = budget.get("window", "all_time")
        since = _window_since(window)
        usage = self.get_project_usage(project_id, since=since)
        total_cost = usage["total_cost_usd"]

        if limit_usd is None or total_cost is None:
            return BudgetStatus(
                project_id=project_id,
                level="unknown",
                pct_used=None,
                limit_usd=limit_usd,
                total_cost_usd=total_cost,
                provenance=usage["cost_provenance"],
                window=window,
                actions=[],
            )

        pct_used = (total_cost / limit_usd) * 100 if limit_usd > 0 else 0.0
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

        return BudgetStatus(
            project_id=project_id,
            level=level,
            pct_used=pct_used,
            limit_usd=limit_usd,
            total_cost_usd=total_cost,
            provenance=usage["cost_provenance"],
            window=window,
            actions=actions,
        )

    def pressure(self, project_id: str) -> float:
        """0.0 (no pressure) .. 1.0 (at/over budget). Used by the Router's
        usage_efficiency component. 0.0 whenever budget status is
        "unknown" (no cost data yet) -- absence of data is not pressure."""
        status = self.check_budget(project_id)
        if status.pct_used is None:
            return 0.0
        return max(0.0, min(1.0, status.pct_used / 100))
