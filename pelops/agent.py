"""Pelops agent factory."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.chat_models import init_chat_model

from pelops.callbacks import MetricsCallback
from pelops.config import Settings
from pelops.middleware import LoggingSummarization
from pelops.persona import system_prompt
from pelops.subagents import SUBAGENTS
from pelops.tools import CHAT_TOOLS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "pelops" / "skills"

_log = logging.getLogger("pelops.agent")


def _build_model(model_id: str):
    """Build a chat model from a provider:model string.

    Supported prefixes:
      groq:<model>           -- routed by init_chat_model
      openrouter:<model>     -- ChatOpenAI against openrouter.ai
      <anything else>        -- init_chat_model fallback (provider auto-detect)
    """
    import os

    temperature = 0.1
    if model_id.startswith("openrouter:"):
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "PELOPS_CHAT_MODEL uses openrouter: prefix but OPENROUTER_API_KEY is not set."
            )
        return ChatOpenAI(
            model=model_id.split(":", 1)[1],
            api_key=SecretStr(api_key),
            base_url="https://openrouter.ai/api/v1",
            temperature=temperature,
        )
    return init_chat_model(model=model_id, temperature=temperature)


def _discover_skill_sources() -> list[str]:
    if not SKILLS_DIR.exists():
        return []
    return [str(SKILLS_DIR.relative_to(PROJECT_ROOT))]


@lru_cache(maxsize=2)
def build_agent(restricted: bool = False):
    """Construct the Pelops deep agent.

    Args:
        restricted: When True, removes scheduling tools (`followup`) from
            the toolset. Used when the agent is invoked as the RESULT of a
            follow-up firing -- prevents runaway scheduling-inside-scheduling
            (the Hermes-style guard). The agent in restricted mode can still
            read state via vstash and produce output, but cannot create new
            timed jobs.
    """
    s = Settings.load()
    model = _build_model(s.chat_model)
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    skills = _discover_skill_sources()
    tools = list(CHAT_TOOLS)
    if restricted:
        tools = [t for t in tools if getattr(t, "name", "") != "followup"]
    # LoggingSummarization closes Pelops's "invisible context compression"
    # gap: when the chat history grows past the trigger threshold, the
    # middleware summarizes older messages AND saves the summary to vstash
    # so future Pelops can recall what was rolled away. Trigger at 60
    # messages or 12k tokens (conservative -- our typical chat has < 20
    # turns).
    summarizer = LoggingSummarization(
        model=model,
        trigger=[("messages", 60), ("tokens", 12_000)],
        keep=("messages", 20),
    )
    return create_deep_agent(
        model=model,
        tools=tools,
        subagents=SUBAGENTS,
        system_prompt=system_prompt(),
        backend=backend,
        skills=skills,
        middleware=[summarizer],
    )


def ask(
    message: str,
    history: list[dict] | None = None,
    restricted: bool = False,
    source: str = "chat",
) -> str:
    """Synchronous helper for one-shot questions.

    Set `restricted=True` when invoking the agent from a follow-up trigger,
    to prevent the agent from scheduling further follow-ups recursively.

    `source` tags the metric records so we can later see "how much did
    briefings cost this week" vs "how much did chat cost".
    """
    agent = build_agent(restricted=restricted)
    messages = list(history or [])
    messages.append({"role": "user", "content": message})
    result = agent.invoke(
        {"messages": messages},
        config={"callbacks": [MetricsCallback(source=source)]},
    )
    final = result["messages"][-1]
    return getattr(final, "content", None) or str(final)
