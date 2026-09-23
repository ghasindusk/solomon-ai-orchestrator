"""Eval Suite (v0.4 Phase 7 Reliability & Evaluation, DECISIONS.md D31).
Formal Spec v0.4 section 18: "Eval: deterministic and integration suites
for routing, context firewall, safety, recovery, addons and completion
semantics."

Thin slice (D1): rather than building a second, parallel test framework,
this categorizes the EXISTING pytest suite by file and shells out to
pytest per category, reporting pass/fail counts. Files that don't map
cleanly onto one of the six named categories are listed separately under
"other" rather than being force-fit into one.

Module named eval_suite.py, not eval.py, to avoid shadowing the Python
builtin `eval` on `import eval`-style tooling.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[2] / "tests"

CATEGORY_FILES: dict[str, list[str]] = {
    "routing": ["test_router.py", "test_replay.py", "test_learning.py"],
    "context_firewall": ["test_knowledge.py", "test_context_leak.py"],
    "safety": ["test_policy.py", "test_risk.py", "test_approvals.py"],
    "recovery": ["test_state.py", "test_sandbox_clone.py", "test_worktree.py"],
    "addons": ["test_addon_manager.py", "test_mcp_server.py"],
    "completion": ["test_verification.py", "test_execution.py", "test_orchestration.py", "test_models.py"],
}

_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error(?:s)?)")


@dataclass
class CategoryResult:
    category: str
    passed: int = 0
    failed: int = 0
    errors: int = 0
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class EvalReport:
    categories: list[CategoryResult] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.categories)


def _run_pytest(paths: list[Path], cwd: Path) -> CategoryResult:
    """Runs pytest against `paths`, parsed from its terse (-q) summary
    line. Never raises on a test failure -- a failing category is a
    normal eval result, not a crash; only a missing pytest/interpreter
    would raise, which is appropriate to surface."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *[str(p) for p in paths]],
        cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
    )
    passed = failed = errors = 0
    for line in proc.stdout.splitlines():
        for count, label in _SUMMARY_RE.findall(line):
            if label == "passed":
                passed = int(count)
            elif label == "failed":
                failed = int(count)
            elif label.startswith("error"):
                errors = int(count)
    return CategoryResult(category="", passed=passed, failed=failed, errors=errors, exit_code=proc.returncode)


def run_eval(
    category_files: dict[str, list[str]] | None = None,
    tests_dir: Path | None = None,
) -> EvalReport:
    """Runs each category's test files as its own pytest invocation and
    collects the results. `tests_dir` defaults to this repo's tests/
    directory; overridable so this function itself stays testable without
    recursively re-running the whole real suite."""
    category_files = category_files if category_files is not None else CATEGORY_FILES
    tests_dir = tests_dir or _TESTS_DIR
    repo_root = tests_dir.parent

    report = EvalReport()
    mapped = {f for files in category_files.values() for f in files}
    for category, filenames in category_files.items():
        paths = [tests_dir / f for f in filenames if (tests_dir / f).exists()]
        if not paths:
            continue
        result = _run_pytest(paths, cwd=repo_root)
        result.category = category
        report.categories.append(result)

    if tests_dir.exists():
        report.other_files = sorted(
            p.name for p in tests_dir.glob("test_*.py") if p.name not in mapped
        )
    return report
