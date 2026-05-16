"""Telegram transport for Pelops.

Two roles:
  1. Long-polling bot that answers messages from the owner.
  2. push_to_owner() -- a one-shot send used by scheduler jobs to nudge Jay
     when something autonomous finishes.

Run with:
    python -m pelops.telegram_bot
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from pelops.agent import build_agent
from pelops.autoschedule import init_scheduler
from pelops.config import Settings

log = logging.getLogger("pelops.telegram")

TG_API = "https://api.telegram.org"
MAX_MSG = 4000  # Telegram cap is 4096 chars; leave headroom for prefixes


def _split(text: str, limit: int = MAX_MSG) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out


def push_to_owner(label: str, body: str) -> None:
    """Synchronously push a message to the configured owner chat.

    Safe to call from non-async contexts (the scheduler is sync). Uses httpx
    directly so we do not have to spin up an event loop just to send one msg.
    Sends as plain text -- agent output is not guaranteed to be valid markdown.
    """
    s = Settings.load()
    if not s.telegram_bot_token or not s.telegram_owner_chat_id:
        log.debug("telegram not configured, skipping push")
        return
    header = f"[Pelops -- {label}]\n\n"
    bot_token = s.telegram_bot_token.get_secret_value()
    for chunk in _split(header + body):
        r = httpx.post(
            f"{TG_API}/bot{bot_token}/sendMessage",
            json={
                "chat_id": s.telegram_owner_chat_id,
                "text": chunk,
            },
            timeout=10.0,
        )
        if r.status_code >= 400:
            log.warning("telegram push failed: %s %s", r.status_code, r.text[:200])
            return


def _build_dispatcher(bot: Bot) -> Dispatcher:
    s = Settings.load()
    dp = Dispatcher()
    agent = build_agent()

    def _owner_only(msg: Message) -> bool:
        if s.telegram_owner_chat_id is None:
            log.warning(
                "TELEGRAM_OWNER_CHAT_ID not set. Your chat_id is: %s "
                "-- add it to .env and restart to enable owner-only mode.",
                msg.chat.id,
            )
            return True
        if msg.chat.id != s.telegram_owner_chat_id:
            log.warning(
                "Ignoring message from chat_id=%s (configured owner=%s). "
                "If this is YOUR chat, update TELEGRAM_OWNER_CHAT_ID in .env.",
                msg.chat.id,
                s.telegram_owner_chat_id,
            )
            return False
        return True

    @dp.message(Command("start"))
    async def cmd_start(msg: Message) -> None:
        if not _owner_only(msg):
            return
        await msg.answer(
            "Woof! Soy Pelops. Hablame y te respondo. "
            "Comandos: /briefing (forzar brief ahora), /memory (cuantas notas)."
        )

    @dp.message(Command("briefing"))
    async def cmd_briefing(msg: Message) -> None:
        if not _owner_only(msg):
            return
        await msg.answer("Generando briefing...")
        from pelops.scheduler import job_briefing

        await asyncio.to_thread(job_briefing)
        # job_briefing already pushes via _push_to_owner; nothing else to send

    @dp.message(Command("memory"))
    async def cmd_memory(msg: Message) -> None:
        if not _owner_only(msg):
            return
        from pelops.memory import get_memory

        docs = list(get_memory().list())
        lines = [f"*Notas en vstash: {len(docs)}*\n"]
        for d in docs[-10:]:
            title = getattr(d, "title", "?")
            tags = getattr(d, "tags", None) or ""
            lines.append(f"- `{title}` _{tags}_")
        await msg.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(msg: Message) -> None:
        if not _owner_only(msg):
            return
        await bot.send_chat_action(msg.chat.id, "typing")
        try:
            reply = await asyncio.to_thread(
                lambda: agent.invoke({"messages": [{"role": "user", "content": msg.text}]})[
                    "messages"
                ][-1].content
            )
        except Exception as exc:
            log.exception("agent invoke failed")
            await msg.answer(f"Algo trono: {exc}", parse_mode=None)
            return
        for chunk in _split(reply or "(empty response)"):
            await msg.answer(chunk, parse_mode=None)

        from pelops.tools import record_chat_turn

        record_chat_turn(msg.text or "", reply or "", source="telegram")

    return dp


async def main() -> None:
    from pelops.logging_config import configure as configure_logging

    configure_logging(level=logging.INFO)
    s = Settings.load()
    if not s.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN missing. Create a bot via @BotFather and put "
            "the token in .env (TELEGRAM_BOT_TOKEN=...). "
            "Then send /start to your bot, capture your chat_id, and set "
            "TELEGRAM_OWNER_CHAT_ID=<id>."
        )

    # Start the local scheduler. It runs ALL the background work:
    # - followup poller (claims due rows from the cross-process jobs table)
    # - watcher poller (event-driven change detection)
    # - cron jobs (ingest / briefing / consolidate / pulse / health)
    # Keeping everything in the bot process means there is one place to
    # launch and one place to debug. The previous design split crons into
    # a separate `python -m pelops.scheduler` process, which led to silent
    # gaps when that process was not running.
    from pelops.scheduler import (
        register_cron_jobs,
        register_followup_poller,
        register_watcher_poller,
    )

    sched = init_scheduler(blocking=False)
    register_cron_jobs(sched)
    register_followup_poller(sched, interval_seconds=15)
    register_watcher_poller(sched, interval_seconds=60)
    sched.start()
    log.info(
        "scheduler started: cron jobs (briefing/pulse/ingest/consolidate/health) "
        "+ followup poller (15s) + watcher poller (60s)"
    )

    bot = Bot(
        token=s.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = _build_dispatcher(bot)
    log.info("Pelops Telegram bot starting (owner_chat_id=%s)", s.telegram_owner_chat_id)
    try:
        await dp.start_polling(bot)
    finally:
        sched.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
