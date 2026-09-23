"""Risk classification -- lean Phase 5 slice (Architecture doc section 8
safety levels L0-L4 / FR-16 Human Approval).

Deliberately a simple keyword heuristic, not a learned classifier: this
is meant to be conservative and auditable (a human can read the rule
that fired), not maximally accurate. Flagged as a first pass -- expect
false positives/negatives; refine the keyword lists as real usage shows
gaps, and prefer over-flagging (HIGH when unsure) to under-flagging.
"""

from __future__ import annotations

from .models import Risk

# L4: privileged OS changes, external publication (architecture doc section 8)
_CRITICAL_KEYWORDS = [
    "force push", "force-push", "--force", "rm -rf", "drop table", "drop database",
    "delete all", "publish", "release to production", "deploy to production",
    "sudo", "chmod 777", "revoke", "delete repository", "delete branch",
]
# L3: broad deletion / large migration
_HIGH_KEYWORDS = [
    "delete", "remove", "migrate", "migration", "overwrite", "reset --hard",
    "drop ", "truncate", "bulk update", "mass update", "credential", "secret",
    "api key", "password",
]
# L1/L2 signals that argue for LOW even if something above also matched loosely
_LOW_KEYWORDS = [
    "read", "analyze", "summarize", "search", "explain", "review", "document",
    "list", "show", "describe",
]


def classify_risk(task_type: str, description: str) -> Risk:
    text = f"{task_type} {description}".lower()

    if any(kw in text for kw in _CRITICAL_KEYWORDS):
        return Risk.CRITICAL
    if any(kw in text for kw in _HIGH_KEYWORDS):
        return Risk.HIGH
    if any(kw in text for kw in _LOW_KEYWORDS):
        return Risk.LOW
    return Risk.NORMAL
