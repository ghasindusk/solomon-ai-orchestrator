"""Octavryn SI v0.5 R8: cross-environment matrix (spec 12 "Environment
matrix"). Provider presence is simulated with the adapter registry: each
present provider is a recording fake, each absent one is disabled and
comes back as MissingAdapter. Nothing real is executed."""

import re
import sys
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import pytest

from solomon import cli, state as state_mod
from solomon.adapters import registry as adapter_registry
from solomon.adapters.base import AdapterHealth, AgentAdapter
from solomon.result import TaskResult
from solomon.state import StateStore

ALL = ["claude_code", "codex", "localai_ollama", "antigravity"]


class Fake(AgentAdapter):
    calls: list = []

    def __init__(self, name):
        self.name = name

    def health(self):
        return AdapterHealth(True, "fake")

    def execute(self, task, prompt, timeout_s=600):
        Fake.calls.append(self.name)
        now = self._now()
        return TaskResult(task_id=task.task_id, status="RESULT_RECEIVED", summary="ok", agent=self.name,
                          started_at=now, finished_at=now)


@pytest.fixture
def providers(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "_DEFAULT_DB_PATH", tmp_path / "m.sqlite3")
    monkeypatch.setattr(cli, "get_gpu_telemetry", lambda: None)
    Fake.calls = []

    def setup(present):
        for name in ALL:
            adapter_registry.unregister_adapter(name)
        for name in present:
            adapter_registry.register_adapter(name, lambda cwd=None, n=name: Fake(n))
        adapter_registry.set_disabled(set(ALL) - set(present))

    yield setup
    for name in ALL:
        adapter_registry.unregister_adapter(name)
    adapter_registry.set_disabled(set())


MATRIX = [
    # (present providers, role, expected exit code, adapter expected to run)
    (ALL, "coder", 0, None),
    (["codex"], "coder", 0, "codex"),
    (["localai_ollama"], "coder", 1, None),                    # local-only: no coder, graceful
    (["localai_ollama"], "knowledge_curator", 0, "localai_ollama"),
    (["claude_code"], "reviewer", 0, "claude_code"),           # single cloud adapter
    (["claude_code", "codex", "antigravity"], "knowledge_curator", 1, None),  # cloud-only, local skill
    (["codex", "localai_ollama"], "coder", 0, "codex"),        # mixed cloud/local
    ([], "coder", 1, None),                                     # no provider at all
]


@pytest.mark.parametrize("present,role,rc,expected", MATRIX)
def test_route_and_run_matrix(providers, present, role, rc, expected):
    providers(present)
    assert cli.main(["route-and-run", "--role", role, "--project-id", "p1", "--prompt", "summarize notes"]) == rc
    if expected:
        assert Fake.calls == [expected]
    else:
        assert len(Fake.calls) == (1 if rc == 0 else 0)
    assert all(c in present for c in Fake.calls)


@pytest.mark.parametrize("cmd", [
    ["skills"],
    ["surfaces"],
    ["intelligences"],
    ["governance-check", "--project-id", "p1", "--prompt", "hello"],
    ["migrate", "status"],
])
def test_core_commands_work_with_no_provider(providers, cmd):
    providers([])
    assert cli.main(cmd) == 0


def test_intelligences_refresh_with_no_provider_marks_all_unavailable(providers, tmp_path):
    providers([])
    assert cli.main(["intelligences", "--refresh"]) == 0
    rows = StateStore(tmp_path / "m.sqlite3").list_intelligences()
    assert {r["availability"] for r in rows} == {"unavailable"}


def test_provider_removed_after_registration_is_skipped_not_crashed(providers):
    providers(["claude_code", "codex"])
    assert cli.main(["intelligences", "--refresh"]) == 0
    providers(["codex"])
    assert cli.main(["route-and-run", "--role", "coder", "--project-id", "p1", "--prompt", "summarize"]) == 0
    assert Fake.calls == ["codex"]


def test_core_modules_do_not_import_named_providers():
    """Spec 12 DoD "no named AI required by Core": only adapters/ may import
    a provider integration module."""
    pattern = re.compile(r"from \.adapters\.(claude_code|codex_adapter|ollama_adapter|antigravity_adapter)\b"
                         r"|import (claude_code|codex_adapter|ollama_adapter|antigravity_adapter)\b")
    offenders = []
    for path in (SRC / "solomon").glob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []
