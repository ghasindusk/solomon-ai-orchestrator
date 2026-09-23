"""Session-wide test fixtures (v0.4 Release Candidate step 7 follow-up,
DECISIONS.md D38).

A fresh checkout has no real 03_Policies/GLOBAL_POLICY.yaml or
04_Config_Schemas/projects.registry.yaml -- both are gitignored (they
hold local paths, see README.md "Getting started"). Several modules
default to loading them when no explicit path is given
(PolicyEngine(), ProjectRegistry(), and execution.execute_with_fallback
internally constructing a PolicyEngine() when no policy is passed), so
a subset of tests that exercise those defaults need *something* there.

This was previously masked on every machine this suite had actually
been run on, because a real GLOBAL_POLICY.yaml/projects.registry.yaml
happened to already exist on disk -- a real portability bug only
surfaced by testing from a genuinely clean checkout (v0.4 Release
Candidate step 7).

Bootstraps from the .example.yaml counterparts if missing -- the exact
same copy the README already tells a new contributor to make by hand,
just done automatically for the test session rather than invented new
config. A no-op wherever the real files already exist (e.g. this
developer's own machine).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BOOTSTRAP_PAIRS = [
    (
        _REPO_ROOT / "03_Policies" / "GLOBAL_POLICY.example.yaml",
        _REPO_ROOT / "03_Policies" / "GLOBAL_POLICY.yaml",
    ),
    (
        _REPO_ROOT / "04_Config_Schemas" / "projects.registry.example.yaml",
        _REPO_ROOT / "04_Config_Schemas" / "projects.registry.yaml",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def bootstrap_example_configs():
    for example_path, real_path in _BOOTSTRAP_PAIRS:
        if not real_path.exists() and example_path.exists():
            real_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example_path, real_path)
    yield
