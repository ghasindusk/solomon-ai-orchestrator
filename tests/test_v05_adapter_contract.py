"""v0.5 R4: the Ollama adapter implements the full contract; normalize/
prepare are pure and testable without a running model. CLI adapters
still report the unsplit parts as explicit Unsupported."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.adapters.base import is_unsupported
from solomon.adapters.ollama_adapter import OllamaAdapter
from solomon.adapters.registry import load_adapter
from solomon.models import Task
from solomon.result import UsageProvenance


def task():
    return Task(goal_id="g", project_id="p", type="t", role="knowledge_curator", definition_of_done=["result_recorded"])


def test_prepare_request_is_pure_and_exact():
    a = OllamaAdapter(base_url="http://localhost:11434", model="m")
    req = a.prepare_request(task(), "hello")
    assert req == {"url": "http://localhost:11434/api/generate", "method": "POST",
                   "headers": {"Content-Type": "application/json"},
                   "json": {"model": "m", "prompt": "hello", "stream": False}}


def test_normalize_result_and_usage_known_vs_unknown():
    a = OllamaAdapter()
    r = a.normalize_result({"response": "hi", "prompt_eval_count": 3, "eval_count": 5}, task())
    assert r.status == "RESULT_RECEIVED" and r.summary == "hi"
    assert r.usage.input_tokens == 3 and r.usage.provenance == UsageProvenance.CLI_REPORTED
    r2 = a.normalize_result({"response": "hi"}, task())
    assert r2.usage.provenance == UsageProvenance.UNKNOWN and r2.usage.input_tokens is None


def test_normalize_result_errors_are_failed_not_success():
    a = OllamaAdapter()
    assert a.normalize_result({"error": "model not found"}, task()).status == "FAILED"
    assert a.normalize_result(["not", "a", "dict"], task()).status == "FAILED"
    assert a.normalize_result({"response": ""}, task()).summary == "(no output)"


def test_execute_unreachable_server_fails_cleanly():
    a = OllamaAdapter(base_url="http://127.0.0.1:9", timeout_connect_s=1)
    r = a.execute(task(), "x", timeout_s=2)
    assert r.status == "FAILED" and r.uncertainties


def test_cli_adapters_report_unsplit_operations_as_unsupported():
    for name in ("claude_code", "codex", "antigravity"):
        a = load_adapter(name)
        assert is_unsupported(a.prepare_request(task(), "x"))
        assert is_unsupported(a.probe_capability("coding"))
    assert not is_unsupported(OllamaAdapter().cleanup())
