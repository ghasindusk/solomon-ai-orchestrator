import json
import subprocess
import sys
import pathlib
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solomon.models import Task
from solomon.adapters.claude_code import ClaudeCodeAdapter
from solomon.adapters.codex_adapter import CodexAdapter
from solomon.adapters.ollama_adapter import OllamaAdapter
from solomon.adapters.antigravity_adapter import AntigravityAdapter
from solomon.adapters.base import NO_BACKGROUND_SUFFIX


def make_task() -> Task:
    return Task(
        goal_id="g1",
        project_id="p1",
        type="test",
        role="coder",
        definition_of_done=["x"],
    )


# --- claude_code ---

def test_claude_code_success():
    adapter = ClaudeCodeAdapter()
    stdout = json.dumps({"result": "hello", "usage": {"input_tokens": 1, "output_tokens": 2}})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=stdout, stderr=""
        )
        result = adapter.execute(make_task(), "hi")
    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "hello"
    assert result.usage.input_tokens == 1


def test_claude_code_failure_returncode():
    adapter = ClaudeCodeAdapter()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="boom"
        )
        result = adapter.execute(make_task(), "hi")
    assert result.status == "FAILED"


def test_claude_code_appends_no_background_suffix():
    # Regression test for the 2026-09-10 false-success incident: a claude_code
    # invocation returned RESULT_RECEIVED with zero actual changes, its own
    # summary talking about "running in the background now" -- the prompt now
    # carries an explicit instruction against backgrounding as a mitigation.
    adapter = ClaudeCodeAdapter()
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=json.dumps({"result": "ok"}), stderr=""
        )

    with patch("subprocess.run", side_effect=_run):
        adapter.execute(make_task(), "do the thing")

    sent_prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert sent_prompt.startswith("do the thing")
    assert sent_prompt.endswith(NO_BACKGROUND_SUFFIX)


def test_claude_code_timeout():
    adapter = ClaudeCodeAdapter()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        result = adapter.execute(make_task(), "hi", timeout_s=1)
    assert result.status == "FAILED"
    assert "timeout" in result.uncertainties


# --- codex ---

def _codex_jsonl(text: str, input_tokens: int = 100, output_tokens: int = 10) -> str:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}}),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }),
    ]
    return "\n".join(lines)


def test_codex_success():
    adapter = CodexAdapter()

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_codex_jsonl("done", input_tokens=50, output_tokens=5), stderr=""
        )

    with patch("subprocess.run", side_effect=_run):
        result = adapter.execute(make_task(), "do something")
    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "done"
    assert result.usage.input_tokens == 50
    assert result.usage.output_tokens == 5
    assert result.usage.provenance == "CLI_REPORTED"


def test_codex_sends_prompt_via_stdin_not_cli_arg():
    # Regression test: codex previously received the prompt as a trailing
    # CLI arg. On Windows that goes through shell=True -> cmd.exe, which
    # truncates a command-line argument at its first embedded newline --
    # confirmed for real, a multi-line review prompt lost everything
    # after line 1. Prompt must travel via subprocess.run's `input=`
    # (stdin) instead, with "-" as the trailing cmd arg telling codex to
    # read stdin.
    adapter = CodexAdapter()
    captured = {}

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_codex_jsonl("ok"), stderr="")

    multiline_prompt = "line one\nline two\nline three"
    with patch("subprocess.run", side_effect=_run):
        adapter.execute(make_task(), multiline_prompt)

    assert captured["cmd"][-1] == "-"
    assert "--json" in captured["cmd"]
    assert multiline_prompt not in captured["cmd"]
    assert captured["input"].startswith(multiline_prompt)  # suffix (NO_BACKGROUND_SUFFIX) follows


def test_codex_appends_no_background_suffix():
    adapter = CodexAdapter()
    captured = {}

    def _run(cmd, **kwargs):
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_codex_jsonl("ok"), stderr="")

    with patch("subprocess.run", side_effect=_run):
        adapter.execute(make_task(), "do something")

    assert captured["input"].startswith("do something")
    assert captured["input"].endswith(NO_BACKGROUND_SUFFIX)


def test_codex_failure_returncode():
    adapter = CodexAdapter()

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="err")

    with patch("subprocess.run", side_effect=_run):
        result = adapter.execute(make_task(), "do something")
    assert result.status == "FAILED"


def test_codex_timeout():
    adapter = CodexAdapter()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1)):
        result = adapter.execute(make_task(), "do something", timeout_s=1)
    assert result.status == "FAILED"


def test_codex_malformed_jsonl_lines_are_skipped_not_fatal():
    adapter = CodexAdapter()
    stdout = "not json\n" + _codex_jsonl("done") + "\n{broken json\n"

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

    with patch("subprocess.run", side_effect=_run):
        result = adapter.execute(make_task(), "do something")
    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "done"


def test_codex_sandbox_workaround_runs_in_clone_and_syncs_back(monkeypatch):
    import solomon.adapters.codex_adapter as codex_mod

    monkeypatch.setattr(codex_mod, "is_incompatible_path", lambda path: True)
    monkeypatch.setattr(codex_mod, "local_clone_path", lambda project_id: pathlib.Path("C:/clone/proj"))

    sync_calls = []

    class FakeSync:
        def __init__(self, ok, detail=""):
            self.ok = ok
            self.detail = detail

    def fake_sync_before(original_repo, clone_path):
        sync_calls.append(("before", str(original_repo), str(clone_path)))
        return FakeSync(True)

    def fake_sync_after(original_repo, clone_path, task_id):
        sync_calls.append(("after", str(original_repo), str(clone_path), task_id))
        return FakeSync(True)

    monkeypatch.setattr(codex_mod, "sync_before", fake_sync_before)
    monkeypatch.setattr(codex_mod, "sync_after", fake_sync_after)

    adapter = codex_mod.CodexAdapter(cwd="G:\\incompatible\\project")
    captured = {}

    def _run(cmd, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_codex_jsonl("done"), stderr="")

    with patch("subprocess.run", side_effect=_run):
        result = adapter.execute(make_task(), "do something")

    assert result.status == "RESULT_RECEIVED"
    assert captured["cwd"] == str(pathlib.Path("C:/clone/proj"))
    assert sync_calls[0] == ("before", "G:\\incompatible\\project", str(pathlib.Path("C:/clone/proj")))
    assert sync_calls[1][0] == "after"
    assert "synced back" in result.summary


def test_codex_sandbox_workaround_fails_fast_when_pre_sync_fails(monkeypatch):
    import solomon.adapters.codex_adapter as codex_mod

    monkeypatch.setattr(codex_mod, "is_incompatible_path", lambda path: True)
    monkeypatch.setattr(codex_mod, "local_clone_path", lambda project_id: pathlib.Path("C:/clone/proj"))

    class FakeSync:
        ok = False
        detail = "clone has uncommitted changes"

    monkeypatch.setattr(codex_mod, "sync_before", lambda original_repo, clone_path: FakeSync())

    adapter = codex_mod.CodexAdapter(cwd="G:\\incompatible\\project")
    with patch("subprocess.run") as mock_run:
        result = adapter.execute(make_task(), "do something")

    assert result.status == "FAILED"
    assert "clone has uncommitted changes" in result.summary
    mock_run.assert_not_called()  # never even tries to invoke codex against the broken sandbox path


def test_codex_sandbox_workaround_fails_when_sync_back_fails(monkeypatch):
    import solomon.adapters.codex_adapter as codex_mod

    monkeypatch.setattr(codex_mod, "is_incompatible_path", lambda path: True)
    monkeypatch.setattr(codex_mod, "local_clone_path", lambda project_id: pathlib.Path("C:/clone/proj"))

    class OkSync:
        ok = True
        detail = ""

    class FailSync:
        ok = False
        detail = "original repo has diverged"

    monkeypatch.setattr(codex_mod, "sync_before", lambda original_repo, clone_path: OkSync())
    monkeypatch.setattr(codex_mod, "sync_after", lambda original_repo, clone_path, task_id: FailSync())

    adapter = codex_mod.CodexAdapter(cwd="G:\\incompatible\\project")

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_codex_jsonl("done"), stderr="")

    with patch("subprocess.run", side_effect=_run):
        result = adapter.execute(make_task(), "do something")

    # codex itself succeeded, but the changes never reached the source of
    # truth repo -- that must surface as a task failure, not a silent success.
    assert result.status == "FAILED"
    assert "original repo has diverged" in result.summary
    assert "subprocess_error" in result.uncertainties


def test_codex_no_json_events_falls_back_to_raw_stdout():
    adapter = CodexAdapter()

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="plain text, no JSON here", stderr="")

    with patch("subprocess.run", side_effect=_run):
        result = adapter.execute(make_task(), "do something")
    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "plain text, no JSON here"
    assert result.usage.provenance == "UNKNOWN"


# --- ollama ---

def test_ollama_success():
    adapter = OllamaAdapter()
    body = json.dumps({"response": "hi there", "eval_count": 5, "prompt_eval_count": 3}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = adapter.execute(make_task(), "hi")
    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "hi there"
    assert result.usage.output_tokens == 5


def test_ollama_failure():
    import urllib.error

    adapter = OllamaAdapter()
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = adapter.execute(make_task(), "hi")
    assert result.status == "FAILED"


# --- antigravity (agy CLI) ---

def test_antigravity_success():
    adapter = AntigravityAdapter()
    stdout = json.dumps(
        {
            "status": "SUCCESS",
            "response": "hi there",
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        }
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["agy"], returncode=0, stdout=stdout, stderr=""
        )
        result = adapter.execute(make_task(), "hi")
    assert result.status == "RESULT_RECEIVED"
    assert result.summary == "hi there"
    assert result.usage.output_tokens == 4


def test_antigravity_agy_reported_failure():
    adapter = AntigravityAdapter()
    stdout = json.dumps({"status": "ERROR", "response": "something went wrong"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["agy"], returncode=0, stdout=stdout, stderr=""
        )
        result = adapter.execute(make_task(), "hi")
    assert result.status == "FAILED"


def test_antigravity_timeout():
    adapter = AntigravityAdapter()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="agy", timeout=1)):
        result = adapter.execute(make_task(), "hi", timeout_s=1)
    assert result.status == "FAILED"
    assert "timeout" in result.uncertainties
