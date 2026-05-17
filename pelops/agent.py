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
from pelops.middleware import PROSE_SUMMARY_PROMPT, LoggingSummarization
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


def _build_checkpointer():
    """Build the agent state checkpointer.

    Returns a `DualModeSqliteSaver` -- a sync `SqliteSaver` whose async
    methods delegate to the sync ones via `asyncio.to_thread`. This is
    the trick that lets the SAME checkpointer back both Telegram (sync
    `invoke`) and Chainlit (async `astream_events`) without us turning
    `build_agent` into an async function. See `pelops/checkpointer.py`.

    Returns None if the langgraph-checkpoint-sqlite dependency is
    missing; the agent will still run, just without persistent state.
    """
    try:
        from pelops.checkpointer import build as build_saver
    except ImportError:
        _log.warning("langgraph-checkpoint-sqlite not installed; checkpointer disabled")
        return None
    s = Settings.load()
    db_path = Path(s.vstash_db).parent / "agent_state.db"
    return build_saver(db_path)


@lru_cache(maxsize=2)
def build_agent(restricted: bool = False):
    """Construct the Pelops deep agent.

    Args:
        restricted: When True, removes scheduling tools (`followup`) from
            the toolset AND disables `interrupt_on`. Used when the agent
            is invoked as the RESULT of a follow-up firing -- prevents
            both runaway scheduling and useless approval prompts (there
            is nobody around to approve in a cron-driven flow).
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
    # middleware summarizes older messages AND saves the summary to vstash.
    summarizer = LoggingSummarization(
        model=model,
        trigger=[("messages", 60), ("tokens", 12_000)],
        keep=("messages", 20),
        summary_prompt=PROSE_SUMMARY_PROMPT,
    )

    # NOTE: interrupt_on={"vstash_remember": True} is intentionally OFF.
    # When enabled, the framework pauses agent execution before the tool
    # call and returns whatever partial output it had -- the user sees a
    # truncated response and the state hangs unresumed (we don't have an
    # approval UI in Chainlit or Telegram yet). Restore this once those
    # transports have a "resume" handler -- see HumanInTheLoop docs.

    return create_deep_agent(
        model=model,
        tools=tools,
        subagents=SUBAGENTS,
        system_prompt=system_prompt(),
        backend=backend,
        skills=skills,
        middleware=[summarizer],
        checkpointer=_build_checkpointer(),
    )


def ask(
    message: str,
    history: list[dict] | None = None,
    restricted: bool = False,
    source: str = "chat",
    thread_id: str | None = None,
    recursion_limit: int = 25,
) -> str:
    """Synchronous helper for one-shot questions.

    `thread_id` ties this invocation to a checkpointer thread for state
    resume. Passing None uses a per-source default ("default-{source}")
    which is fine for our single-user setup but means autonomous flows
    of the same kind share state -- normally desirable for cron jobs.

    `recursion_limit` caps the number of model->tools->model iterations
    LangGraph will run before stopping. Default 25 matches LangGraph's
    own default. Heartbeats pass a higher value (50) because they often
    chain several recall calls + a wiki_write + a final synthesis;
    hitting the cap mid-chain leaves the agent with `content=''` and
    no final disposition -- exactly the failure mode we saw on
    2026-05-16 at 12:30 and 12:45.
    """
    agent = build_agent(restricted=restricted)
    messages = list(history or [])
    messages.append({"role": "user", "content": message})
    result = agent.invoke(
        {"messages": messages},
        config={
            "callbacks": [MetricsCallback(source=source)],
            "configurable": {"thread_id": thread_id or f"default-{source}"},
            "recursion_limit": recursion_limit,
        },
    )
    final = result["messages"][-1]
    return getattr(final, "content", None) or str(final)
