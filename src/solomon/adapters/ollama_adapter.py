"""localai_ollama adapter: wraps the local Ollama HTTP API.

Capabilities per 04_Config_Schemas/agents.example.yaml: knowledge, rag,
summarization, classification, context_compression.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..models import Task
from ..result import TaskResult, Usage, UsageProvenance
from ..descriptors import Locality
from .base import AdapterDeclaration, AdapterHealth, AgentAdapter


class OllamaAdapter(AgentAdapter):
    name = "localai_ollama"
    declaration = AdapterDeclaration(
        name="localai_ollama",
        display_name="Ollama (local)",
        adapter_type="http",
        provider="ollama",
        locality=Locality.LOCAL,
        capabilities=['knowledge', 'rag', 'summarization', 'classification', 'context_compression'],
        credentials="none",
        telemetry="tokens",
        mcp_tool_support=False,
    )

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:7b",
        timeout_connect_s: int = 5,
    ):
        self.base_url = base_url
        self.model = model
        self.timeout_connect_s = timeout_connect_s

    def health(self) -> AdapterHealth:
        try:
            with urllib.request.urlopen(  # nosec B310 - configured local http base_url
                f"{self.base_url}/api/tags", timeout=self.timeout_connect_s
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            names = [m.get("name") for m in data.get("models", [])]
            return AdapterHealth(True, f"models: {', '.join(names)}")
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            return AdapterHealth(False, str(exc))

    def list_models(self) -> list[str]:
        """Read-only GET /api/tags. An unreachable server returns [] and
        health() reports why; it is not Unsupported, since Ollama does
        implement model listing."""
        try:
            with urllib.request.urlopen(  # nosec B310 - configured local http base_url
                f"{self.base_url}/api/tags", timeout=self.timeout_connect_s
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
        except Exception:  # noqa: BLE001 - discovery must not raise
            return []

    # --- v0.5 contract (spec 05): execute() = prepare -> send -> normalize ---

    def prepare_request(self, task: Task, prompt: str) -> dict:
        """Pure: the exact HTTP request execute() would send. Nothing is sent."""
        return {
            "url": f"{self.base_url}/api/generate",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "json": {"model": self.model, "prompt": prompt, "stream": False},
        }

    def normalize_usage(self, raw: dict) -> Usage:
        """Token counts only when Ollama reported both; otherwise UNKNOWN
        (never 0)."""
        if isinstance(raw, dict) and "eval_count" in raw and "prompt_eval_count" in raw:
            return Usage(
                input_tokens=raw.get("prompt_eval_count"),
                output_tokens=raw.get("eval_count"),
                provenance=UsageProvenance.CLI_REPORTED,
            )
        return Usage(provenance=UsageProvenance.UNKNOWN)

    def normalize_result(self, raw: dict, task: Task, started: str | None = None, raw_text: str = "") -> TaskResult:
        if not isinstance(raw, dict):
            return TaskResult(task_id=task.task_id, status="FAILED", summary="ollama returned a non-object response",
                              agent=self.name, started_at=started or self._now(),
                              uncertainties=["unparseable response shape"])
        if raw.get("error"):
            return TaskResult(task_id=task.task_id, status="FAILED", summary=f"ollama error: {raw['error']}"[:500],
                              agent=self.name, started_at=started or self._now(),
                              uncertainties=[str(raw["error"])[:500]])
        summary_text = str(raw.get("response", ""))
        return TaskResult(
            task_id=task.task_id,
            status="RESULT_RECEIVED",
            summary=summary_text[:2000] if summary_text else "(no output)",
            agent=self.name,
            started_at=started or self._now(),
            usage=self.normalize_usage(raw),
            raw_output=raw_text[:5000],
        )

    def cleanup(self) -> None:
        """Stateless HTTP client: nothing to clean up (supported, a no-op)."""
        return None

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        started = self._now()
        req = self.prepare_request(task, prompt)
        request = urllib.request.Request(
            req["url"],
            data=json.dumps(req["json"]).encode("utf-8"),
            headers=req["headers"],
            method=req["method"],
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # nosec B310 - local configured base_url
                raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"ollama execution failed: {exc}",
                agent=self.name,
                started_at=started,
                uncertainties=[str(exc)[:500]],
            )
        except Exception as exc:  # noqa: BLE001
            return TaskResult(
                task_id=task.task_id,
                status="FAILED",
                summary=f"ollama execution failed: {exc}",
                agent=self.name,
                started_at=started,
                uncertainties=[str(exc)[:500]],
            )
        return self.normalize_result(parsed, task, started=started, raw_text=raw)
