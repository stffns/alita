"""Pelops agent factory."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from pelops.callbacks import MetricsCallback
from pelops.config import Settings
from pelops.middleware import PROSE_SUMMARY_PROMPT, LoggingSummarization
from pelops.models import build_model
from pelops.persona import system_prompt
from pelops.subagents import SUBAGENTS
from pelops.tools import CHAT_TOOLS, HEARTBEAT_TOOLS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "pelops" / "skills"

_log = logging.getLogger("pelops.agent")


# `_build_model` was moved to `pelops.models.build_model` so the
# `pelops.subagents` module can import it without a circular import
# back to this file (see `pelops/models.py`). Keep the old name as a
# thin alias in case anything outside the package still references it.
_build_model = build_model


def _discover_skill_sources() -> list[str]:
    """Return the list of directories deepagents should scan for skills.

    Priority order: VAULT first, then CODE. The vault wins for
    duplicate slugs so Alita can refine a code-shipped skill by
    writing an improved version via `skill_write` -- without that
    priority, refinements would be shadowed by the original code
    version, breaking the REFINE workflow documented in the
    skill-builder skill.

      1. `<wiki_dir>/skills/` -- Alita-authored skills written via the
         `skill_write` tool. Auto-discovered at agent build time.
      2. `pelops/skills/` -- code-shipped skills as the floor.

    Both paths are absolute to avoid surprises depending on the
    process's current working directory. Either path is optional;
    missing directories are skipped silently.
    """
    sources: list[str] = []
    try:
        wiki_skills = Settings.load().wiki_dir / "skills"
    except Exception:
        wiki_skills = None
    if wiki_skills is not None and wiki_skills.exists():
        sources.append(str(wiki_skills.resolve()))
    if SKILLS_DIR.exists():
        sources.append(str(SKILLS_DIR.resolve()))
    return sources


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


@lru_cache(maxsize=4)
def build_agent(mode: str = "full"):
    """Construct the Pelops deep agent.

    Args:
        mode: Which toolset variant to build.
            * "full"       -- CHAT_TOOLS, the default for user-facing chat.
            * "restricted" -- CHAT_TOOLS minus `followup`. For cron-driven
              invocations that must not recursively schedule more
              follow-ups (e.g. the followup runner itself, session
              snapshots).
            * "heartbeat"  -- HEARTBEAT_TOOLS, the minimal subset used by
              `job_heartbeat`. Halves the per-call prompt overhead vs.
              the full toolset (~5k vs ~11k tokens of tool definitions).
    """
    s = Settings.load()
    model = _build_model(s.chat_model)
    # virtual_mode=False so the deepagents-native filesystem tools
    # (read_file/write_file/edit_file/ls/glob/grep) operate on real disk
    # under PROJECT_ROOT. This enables self-modification: Alita can
    # read and edit her own Python code in `pelops/*.py`, `scripts/`,
    # `Makefile`, etc. Blast radius is bounded by root_dir -- the wiki
    # vault (`~/Documents/pelops-wiki/`) and everything else outside
    # PROJECT_ROOT remain unreachable through these tools.
    #
    # KNOWN SOFT-LIMIT (not enforced in code): there is no `git_push`
    # or self-restart tool, BUT a bad self-edit to a module that the
    # bot already imports is NOT auto-recovered. The bot is supervised
    # by launchd with `KeepAlive` + `Crashed=true`; an agent-triggered
    # crash (e.g. `code_execute` running `sys.exit(2)` after editing
    # the loaded code) would respawn into the modified code,
    # bypassing the intended "Jay reviews `git status` first" gate.
    # The persona instructs Alita NOT to do this. The guarantee is
    # trust-based, not enforced. A future PR could add a write-block
    # list (refuse edits to `pelops/agent.py`, `pelops/persona.py`,
    # etc) or strip the crash-trigger tools from the toolset.
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=False)
    skills = _discover_skill_sources()
    if mode == "heartbeat":
        tools = list(HEARTBEAT_TOOLS)
    elif mode == "restricted":
        tools = [t for t in CHAT_TOOLS if getattr(t, "name", "") != "followup"]
    else:
        tools = list(CHAT_TOOLS)
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


_last_persona_mtime: float | None = None


def _maybe_invalidate_agent_cache() -> None:
    """Rebuild the cached agent when `persona.md` has changed on disk.

    The agent's system prompt is baked into the graph at construction
    time, and `build_agent` is `@lru_cache`'d. Without this check,
    edits to `persona.md` (by Jay in Obsidian, or by Alita herself via
    `wiki_write`) would not take effect until the bot restarted.

    We track the file's mtime in process memory. When it changes, we
    clear the lru_cache so the NEXT `build_agent()` call reconstructs
    the agent with the fresh persona. The check is one stat() per
    ask() call -- microseconds, no I/O cost worth measuring.
    """
    global _last_persona_mtime
    try:
        s = Settings.load()
        persona_path = s.wiki_dir / "persona.md"
        if not persona_path.exists():
            return
        mtime = persona_path.stat().st_mtime
    except Exception:
        return
    if _last_persona_mtime is None:
        _last_persona_mtime = mtime
        return
    if mtime > _last_persona_mtime:
        _log.info("persona.md mtime changed, rebuilding agent on next call")
        _last_persona_mtime = mtime
        build_agent.cache_clear()


_EMPTY_CONTENT_REPLY = (
    "(Perdi la sintesis final -- la cadena de tools se quedo sin "
    "espacio antes de que escribiera una respuesta. Probemos de "
    "nuevo, o partimos la pregunta en pedazos mas chicos. "
    "/ I lost the final synthesis -- the tool chain ran out of room "
    "before I could write a response. Try asking again, or split the "
    "question into smaller parts.)"
)


def ask(
    message: str,
    history: list[dict] | None = None,
    mode: str = "full",
    source: str = "chat",
    thread_id: str | None = None,
    recursion_limit: int = 100,
) -> str:
    """Synchronous helper for one-shot questions.

    `thread_id` ties this invocation to a checkpointer thread for state
    resume. Passing None uses a per-source default ("default-{source}")
    which is fine for our single-user setup but means autonomous flows
    of the same kind share state -- normally desirable for cron jobs.

    `recursion_limit` caps the number of LangGraph node steps before the
    graph stops. Default 100 (was 50 before sub-agents). The deep /
    vision / researcher sub-agents added in PR #28 each chain inside
    the parent graph -- one `task(...)` call can easily consume 20-30
    nodes -- so a turn that uses two sub-agents needs more headroom
    than the pre-sub-agent chat flow. Started at LangGraph's default
    of 25, bumped to 40 in PR #14, to 50 in PR #15 (wiki-page-creator
    chained research + wiki_write + adoption -- 40 was tight), and to
    100 now (sub-agents).

    On every call we check `persona.md` for changes and rebuild the
    cached agent if the file was edited -- this is what makes wiki
    edits to the persona take effect on the next turn without a
    restart.

    When the underlying model produces an empty final message (which
    happens when the recursion cap is reached mid-chain, or the model
    rate-limits, or just returns no text), we substitute a friendly
    placeholder instead of leaking the raw `AIMessage` repr to the
    caller. The placeholder explains what happened and suggests a
    retry.
    """
    from langchain_core.messages import AIMessage

    _maybe_invalidate_agent_cache()
    agent = build_agent(mode=mode)
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
    # Only an AIMessage is a valid final answer. If the chain stopped
    # mid-flight (e.g., recursion limit hit after a tool call), the
    # last message is a ToolMessage whose `content` is raw tool output
    # and must NOT be surfaced to the user. Treat that as empty too.
    if isinstance(final, AIMessage):
        content = getattr(final, "content", None)
        # content can be str OR a list of content blocks (multi-modal /
        # structured output). For the simple-text case, return as-is.
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list) and content:
            # Join string-valued blocks. Anything non-string is dropped.
            text = "".join(b for b in content if isinstance(b, str)).strip()
            if text:
                return text
    _log.warning(
        "ask: empty/non-AI final message (source=%s, thread=%s, type=%s); "
        "likely recursion_limit hit, tool-loop overshoot, or model refusal",
        source,
        thread_id or f"default-{source}",
        type(final).__name__,
    )
    return _EMPTY_CONTENT_REPLY
