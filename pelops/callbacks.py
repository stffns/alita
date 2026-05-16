"""LangChain callback handlers for Pelops metrics.

Hooks into the agent's LLM and tool events so we record tokens + latency
without changing any user-facing code. Attach to build_agent() once.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler

from pelops import metrics

log = logging.getLogger("pelops.callbacks")


class MetricsCallback(BaseCallbackHandler):
    """Records per-turn and per-tool metrics into the local SQLite table."""

    def __init__(self, source: str = "chat") -> None:
        self.source = source
        self._llm_starts: dict[str, float] = {}
        self._tool_starts: dict[str, tuple[float, str]] = {}

    # -- LLM lifecycle --------------------------------------------------- #

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        self._llm_starts[str(run_id)] = time.time()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        self._llm_starts[str(run_id)] = time.time()

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        started = self._llm_starts.pop(str(run_id), None)
        duration_ms = int((time.time() - started) * 1000) if started else None
        model = None
        prompt_tokens = 0
        completion_tokens = 0

        # Path 1: response.llm_output (Groq, classic OpenAI providers).
        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            model = llm_output.get("model_name") or llm_output.get("model")
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            prompt_tokens = (
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or 0
            )
            completion_tokens = (
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or 0
            )

        # Path 2: message.usage_metadata (langchain >=0.3 standardized field).
        # Path 3: message.response_metadata.token_usage (OpenRouter via ChatOpenAI).
        if not (prompt_tokens or completion_tokens):
            try:
                msg = response.generations[0][0].message
                um = getattr(msg, "usage_metadata", None) or {}
                if um:
                    prompt_tokens = um.get("input_tokens") or prompt_tokens
                    completion_tokens = um.get("output_tokens") or completion_tokens
                rm = getattr(msg, "response_metadata", None) or {}
                if not (prompt_tokens or completion_tokens):
                    tu = rm.get("token_usage") or rm.get("usage") or {}
                    prompt_tokens = (
                        tu.get("prompt_tokens")
                        or tu.get("input_tokens")
                        or prompt_tokens
                    )
                    completion_tokens = (
                        tu.get("completion_tokens")
                        or tu.get("output_tokens")
                        or completion_tokens
                    )
                model = (
                    model
                    or rm.get("model_name")
                    or rm.get("model")
                )
            except Exception:  # noqa: BLE001
                pass

        # Defensive: some providers double the model name when the field
        # appears in both llm_output and response_metadata. Dedupe.
        if model and len(model) > 4 and len(model) % 2 == 0:
            half = len(model) // 2
            if model[:half] == model[half:]:
                model = model[:half]

        metrics.record_turn(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            source=self.source,
        )

    # -- Tool lifecycle -------------------------------------------------- #

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id,
        **kwargs,
    ) -> None:
        name = (serialized or {}).get("name") or "?"
        self._tool_starts[str(run_id)] = (time.time(), name)

    def on_tool_end(self, output: Any, *, run_id, **kwargs) -> None:
        started = self._tool_starts.pop(str(run_id), None)
        if not started:
            return
        t0, name = started
        metrics.record_tool(name, int((time.time() - t0) * 1000), ok=True)

    def on_tool_error(self, error: BaseException, *, run_id, **kwargs) -> None:
        started = self._tool_starts.pop(str(run_id), None)
        if not started:
            return
        t0, name = started
        metrics.record_tool(
            name,
            int((time.time() - t0) * 1000),
            ok=False,
            error=str(error)[:200],
        )
