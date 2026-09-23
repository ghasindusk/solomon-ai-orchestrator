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
from .base import AdapterHealth, AgentAdapter


class OllamaAdapter(AgentAdapter):
    name = "localai_ollama"

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
            with urllib.request.urlopen(
                f"{self.base_url}/api/tags", timeout=self.timeout_connect_s
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            names = [m.get("name") for m in data.get("models", [])]
            return AdapterHealth(True, f"models: {', '.join(names)}")
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            return AdapterHealth(False, str(exc))

    def execute(self, task: Task, prompt: str, timeout_s: int = 600) -> TaskResult:
        started = self._now()
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
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

        summary_text = str(parsed.get("response", ""))
        if "eval_count" in parsed and "prompt_eval_count" in parsed:
            usage = Usage(
                input_tokens=parsed.get("prompt_eval_count"),
                output_tokens=parsed.get("eval_count"),
                provenance=UsageProvenance.CLI_REPORTED,
            )
        else:
            usage = Usage(provenance=UsageProvenance.UNKNOWN)

        return TaskResult(
            task_id=task.task_id,
            status="RESULT_RECEIVED",
            summary=summary_text[:2000] if summary_text else "(no output)",
            agent=self.name,
            started_at=started,
            usage=usage,
            raw_output=raw[:5000],
        )
