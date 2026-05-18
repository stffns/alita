"""Chainlit UI for Pelops.

Run with:
    chainlit run pelops/ui.py -w
"""

from __future__ import annotations

# Load .env into os.environ BEFORE importing anything that depends on env
# vars (langchain reads LANGSMITH_* directly from os.environ -- pydantic
# Settings populates the Settings instance but does NOT touch os.environ,
# so without this langchain tracing silently no-ops).
from dotenv import load_dotenv

load_dotenv()

import asyncio  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import chainlit as cl  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from pelops.agent import build_agent  # noqa: E402
from pelops.callbacks import MetricsCallback  # noqa: E402
from pelops.config import Settings  # noqa: E402


@cl.on_chat_start
async def on_chat_start() -> None:
    s = Settings.load()
    cl.user_session.set("agent", build_agent())
    cl.user_session.set("history", [])
    # Each Chainlit session gets its own checkpointer thread so a refresh
    # of the tab can continue the same conversation.
    cl.user_session.set("thread_id", f"chainlit-{cl.context.session.id}")

    # Inbox catch-up: show as a one-line notification with action buttons,
    # not as a chat message. Pelops's own welcome comes after.
    from pelops import jobs

    recent = jobs.recent_completions(hours=24, limit=10)
    cl.user_session.set("inbox", {r["id"]: r for r in recent})
    if recent:
        actions = [
            cl.Action(
                name="inbox_open",
                payload={"job_id": r["id"]},
                label=f"{(r.get('fired_at') or '')[11:16]} {r.get('label') or '?'}",
            )
            for r in recent
        ]
        actions.append(cl.Action(name="inbox_dismiss", payload={}, label="Dismiss"))
        await cl.Message(
            author="Inbox",
            content=(
                f"You have {len(recent)} follow-up{'s' if len(recent) != 1 else ''} "
                f"that fired in the last 24h. Click to view."
            ),
            actions=actions,
        ).send()

    await cl.Message(
        author="Pelops",
        content=(
            f"Woof! Hi {s.owner}. I'm Pelops -- your research companion.\n"
            f"Focus areas: {', '.join(s.topics) or 'general'}.\n"
            f"Try: *'investiga lo ultimo sobre agentes deepagents'* "
            f"or *'que sabes de mi proyecto X?'*"
        ),
    ).send()

    # Live push: spawn a background task that polls the followups table and
    # surfaces any newly-fired rows into THIS chat. Mirrors Telegram pushes.
    session_start = datetime.now(UTC).isoformat()
    cl.user_session.set("live_seen", {r["id"] for r in recent})
    cl.user_session.set("live_after", session_start)
    # Keep a reference so the task is not garbage-collected mid-loop
    # (per RUF006). The session is short-lived enough that we do not
    # need to manage cancellation explicitly.
    cl.user_session.set("live_pusher_task", asyncio.create_task(_live_pusher()))


async def _live_pusher() -> None:
    """Background loop per Chainlit session.

    Every 15s, query pelops_followups for rows fired AFTER session_start that
    we have not already shown, and render them as in-chat messages.
    """
    from pelops import jobs

    while True:
        try:
            await asyncio.sleep(15)
            seen: set = cl.user_session.get("live_seen") or set()
            after: str = cl.user_session.get("live_after") or ""
            recent = jobs.recent_completions(hours=2, limit=20)
            new = [r for r in recent if r["id"] not in seen and (r.get("fired_at") or "") >= after]
            if not new:
                continue
            # Push oldest-first so the chat reads chronologically.
            for r in reversed(new):
                ts = (r.get("fired_at") or "")[:16].replace("T", " ")
                label = r.get("label") or "?"
                response = r.get("response") or ""
                if not response:
                    continue
                await cl.Message(
                    author="Pelops (push)",
                    content=f"**{ts} UTC -- {label}**\n\n{response}",
                ).send()
                seen.add(r["id"])
            cl.user_session.set("live_seen", seen)
        except asyncio.CancelledError:
            return
        except Exception:
            # Best-effort -- never let the polling loop die.
            await asyncio.sleep(5)


@cl.action_callback("inbox_open")
async def on_inbox_open(action: cl.Action) -> None:
    inbox = cl.user_session.get("inbox") or {}
    job_id = action.payload.get("job_id")
    item = inbox.get(job_id)
    if not item:
        await cl.Message(author="Inbox", content=f"(item {job_id} no longer cached)").send()
        return
    ts = (item.get("fired_at") or "")[:16].replace("T", " ")
    label = item.get("label") or "?"
    status = item.get("status") or "?"
    prompt = item.get("prompt") or ""
    response = item.get("response") or ""
    last_error = item.get("last_error") or ""

    body = [f"**{ts} UTC -- {label}** ({status})", "", f"_prompt:_ {prompt}"]
    if response:
        body.extend(["", response])
    elif last_error:
        body.extend(["", f"_error:_ {last_error}"])
    else:
        body.append("\n_(no response stored -- this fired before response capture was added)_")
    await cl.Message(author="Inbox item", content="\n".join(body)).send()


@cl.action_callback("inbox_dismiss")
async def on_inbox_dismiss(action: cl.Action) -> None:
    cl.user_session.set("inbox", {})
    await cl.Message(author="Inbox", content="Dismissed.").send()


def _to_lc(history: list[dict]):
    out = []
    for m in history:
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        else:
            out.append(AIMessage(content=m["content"]))
    return out


@cl.on_message
async def on_message(message: cl.Message) -> None:
    agent = cl.user_session.get("agent")
    history: list[dict] = cl.user_session.get("history", [])

    messages = [*_to_lc(history), HumanMessage(content=message.content)]

    response = cl.Message(author="Pelops", content="")
    final_text = ""

    thread_id = cl.user_session.get("thread_id") or "chainlit-default"
    # Match the `ask()` chat path's default in pelops.agent (50). LangGraph's
    # built-in default of 25 is too tight for multi-tool chains like
    # "write a snake game" or "create wiki page then adopt orphan" --
    # those hit the cap mid-flight and surface a GraphRecursionError.
    async for event in agent.astream_events(
        {"messages": messages},
        version="v2",
        config={
            "callbacks": [MetricsCallback(source="chainlit")],
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 50,
        },
    ):
        # SummarizationMiddleware invokes the chat model with a
        # `lc_source=summarization` metadata tag when it compresses
        # old turns. Drop EVERY event from that branch -- not just
        # model-stream tokens -- so a future internal tool call from
        # within the summarization path (`on_tool_start`, etc) cannot
        # leak into the chat either. Bug surfaced 2026-05-18.
        md = event.get("metadata") or {}
        if md.get("lc_source") == "summarization":
            continue
        kind = event["event"]
        if kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            token = getattr(chunk, "content", "") or ""
            if token:
                await response.stream_token(token)
                final_text += token
        # Note: we used to surface `on_tool_start` as a `tool <name> ...`
        # message under the reply. Jay flagged 2026-05-18 that it just
        # clutters the chat -- ten "tool glob ..." / "tool ls ..." lines
        # appearing under a single reply add noise without context.
        # The full tool trace is still available in LangSmith for
        # debugging; the UI shows only the final prose.

    await response.send()

    history.append({"role": "user", "content": message.content})
    history.append({"role": "assistant", "content": final_text})
    cl.user_session.set("history", history[-20:])

    # Continuous episodic memory -- save this exchange for future recall.
    from pelops.tools import record_chat_turn

    record_chat_turn(message.content, final_text, source="chainlit")
