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

# Load .env into os.environ BEFORE importing anything that depends on env
# vars (langchain reads LANGSMITH_* directly from os.environ -- pydantic
# Settings reads .env into the Settings instance but does NOT touch
# os.environ, so without this langchain tracing silently no-ops).
from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402
from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.filters import Command  # noqa: E402
from aiogram.types import Message  # noqa: E402

from pelops.agent import build_agent  # noqa: E402
from pelops.autoschedule import init_scheduler  # noqa: E402
from pelops.config import Settings  # noqa: E402

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

    # Cap for inline audio we will accept BEFORE downloading. Telegram
    # voice notes are ~1MB; legitimate forwarded clips fit under 20MB.
    # Past this we refuse via `media.file_size` -- avoids pulling a
    # giant blob into the bot's RAM only to reject it later in stt.
    _AUDIO_INLINE_CAP = 20 * 1024 * 1024

    async def _run_agent_turn(msg: Message, text: str, source: str = "telegram") -> None:
        """Shared turn handler: send `text` to the agent, stream reply back.

        Both `on_text` (typed messages) and `on_voice` (transcribed
        voice notes) end up here. `source` differentiates the two in
        episodic recall so a future "what did Jay say yesterday?"
        query can tell voice apart from text.
        """
        await bot.send_chat_action(msg.chat.id, "typing")
        config = {"configurable": {"thread_id": f"telegram-{msg.chat.id}"}}
        try:
            reply = await asyncio.to_thread(
                lambda: (
                    agent.invoke(
                        {"messages": [{"role": "user", "content": text}]},
                        config=config,
                    )["messages"][-1].content
                )
            )
        except Exception as exc:
            log.exception("agent invoke failed")
            await msg.answer(f"Algo trono: {exc}", parse_mode=None)
            return
        # Defensive: an LLM provider can return a list of content
        # blocks (multimodal) instead of a string, or the chain can
        # end on a ToolMessage whose `content` is bytes. Coerce to a
        # plain string so `_split` (which assumes str) never crashes.
        reply_text = reply if isinstance(reply, str) else str(reply) if reply else ""
        for chunk in _split(reply_text or "(empty response)"):
            await msg.answer(chunk, parse_mode=None)

        # Symmetric voice reply: if Jay sent a voice note, ALSO send
        # the answer as audio. Text is sent first (above) so it lands
        # even if TTS fails -- and so Jay can still skim later. The
        # cap inside `tts.synthesize` keeps audio replies under ~30s.
        if source == "telegram-voice" and reply_text:
            from pelops import tts

            try:
                audio = await tts.synthesize(reply_text)
            except tts.TtsError as exc:
                log.warning("tts failed (continuing with text-only): %s", exc)
            else:
                from aiogram.types import BufferedInputFile

                try:
                    await bot.send_voice(
                        msg.chat.id,
                        BufferedInputFile(audio, filename="alita.ogg"),
                    )
                except Exception:
                    log.exception("send_voice failed (text already sent, ignoring)")

        from pelops.tools import record_chat_turn

        record_chat_turn(text, reply_text, source=source)

    @dp.message(F.text & ~F.text.startswith("/"))
    async def on_text(msg: Message) -> None:
        if not _owner_only(msg):
            return
        await _run_agent_turn(msg, msg.text or "")

    @dp.message(F.voice | F.audio)
    async def on_voice(msg: Message) -> None:
        """Transcribe a voice note (or forwarded audio) and feed the text to the agent.

        Telegram delivers voice notes as `msg.voice` (OGG/Opus) and
        forwarded music files as `msg.audio` (varies). Both expose a
        `file_id`, a `mime_type`, and a `file_size`. We size-check
        BEFORE downloading, pull the bytes via aiogram's `download`,
        hand them to Deepgram, echo the transcript back so Jay can
        confirm what was heard, and then dispatch like a typed message.
        """
        if not _owner_only(msg):
            return
        # Either voice or audio, never both -- aiogram routes one at
        # a time. Voice takes precedence semantically.
        media = msg.voice or msg.audio
        if media is None:  # defensive: filter matched but attrs vanished
            return

        # Reject oversized files BEFORE pulling them into RAM -- the
        # Telegram payload tells us file_size upfront, no reason to
        # download just to refuse.
        size = getattr(media, "file_size", None) or 0
        if size > _AUDIO_INLINE_CAP:
            await msg.answer(
                f"Audio demasiado grande ({size:,} bytes; cap {_AUDIO_INLINE_CAP:,}).",
                parse_mode=None,
            )
            return

        # `bot.download(media)` is the aiogram 3.x helper that handles
        # `get_file` + `download_file` in one call. Returns a BytesIO.
        # Close it explicitly so the 20MB buffer is freed promptly.
        buf = None
        try:
            buf = await bot.download(media)
            audio_bytes = buf.read() if buf is not None else b""
        except Exception as exc:
            log.exception("voice download failed")
            await msg.answer(f"No pude bajar el audio: {exc}", parse_mode=None)
            return
        finally:
            if buf is not None:
                try:
                    buf.close()
                except Exception:
                    pass

        from pelops import stt

        try:
            result = await stt.transcribe(
                audio_bytes,
                mime_type=getattr(media, "mime_type", None) or "audio/ogg",
            )
        except stt.SttError as exc:
            await msg.answer(f"STT: {exc}", parse_mode=None)
            return

        text = (result.get("text") or "").strip()
        if not text:
            await msg.answer(
                "(no entendi nada; mandalo de nuevo o por texto)",
                parse_mode=None,
            )
            return

        # Echo the transcript so Jay can confirm what was heard BEFORE
        # the agent responds. Short voice notes can mis-transcribe and
        # he should see the gap.
        await msg.answer(f">> voz: {text}", parse_mode=None)
        await _run_agent_turn(msg, text, source="telegram-voice")

    @dp.message(F.photo)
    async def on_photo(msg: Message) -> None:
        """Forward a photo (and any caption) to the multimodal agent.

        DeepSeek v4 (the main chat model) is text-only; we cannot just
        attach the image to a normal HumanMessage. Instead we package
        the image as a base64 data URL plus the caption text, hand it
        to the agent, and rely on the `vision` sub-agent (Gemini Flash)
        to actually look at it. The persona tells the main agent to
        call `task("vision", ...)` when the input has an image_url.
        """
        if not _owner_only(msg):
            return
        # Telegram delivers photos as a list of PhotoSize objects with
        # progressively larger resolutions. The last one is the highest
        # quality the sender allowed.
        if not msg.photo:
            return
        photo = msg.photo[-1]
        size = getattr(photo, "file_size", None) or 0
        if size > _AUDIO_INLINE_CAP:  # reuse the 20MB inline cap
            await msg.answer(
                f"Imagen demasiado grande ({size:,} bytes; cap {_AUDIO_INLINE_CAP:,}).",
                parse_mode=None,
            )
            return

        await bot.send_chat_action(msg.chat.id, "typing")
        buf = None
        try:
            buf = await bot.download(photo)
            image_bytes = buf.read() if buf is not None else b""
        except Exception as exc:
            log.exception("photo download failed")
            await msg.answer(f"No pude bajar la imagen: {exc}", parse_mode=None)
            return
        finally:
            if buf is not None:
                try:
                    buf.close()
                except Exception:
                    pass

        import base64

        # Telegram serves photos as JPEG; the data URL signals that
        # to the multimodal model.
        data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode()
        caption = (msg.caption or "").strip()

        # Hand the agent a multimodal HumanMessage. The persona /
        # vision sub-agent description tell it to dispatch via
        # task("vision", ...). The main chat model only sees a text
        # placeholder; the image goes to the sub-agent.
        prompt_blocks = [
            {
                "type": "text",
                "text": caption
                or "Te llego una imagen sin texto. Llama al sub-agente vision para describirla.",
            },
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        config = {"configurable": {"thread_id": f"telegram-{msg.chat.id}"}}
        try:
            from langchain_core.messages import HumanMessage

            reply = await asyncio.to_thread(
                lambda: (
                    agent.invoke(
                        {"messages": [HumanMessage(content=prompt_blocks)]},
                        config=config,
                    )["messages"][-1].content
                )
            )
        except Exception as exc:
            log.exception("photo agent invoke failed")
            await msg.answer(f"Algo trono con la imagen: {exc}", parse_mode=None)
            return
        reply_text = reply if isinstance(reply, str) else str(reply) if reply else ""
        for chunk in _split(reply_text or "(empty response)"):
            await msg.answer(chunk, parse_mode=None)

        from pelops.tools import record_chat_turn

        record_chat_turn(
            caption or "(imagen sin caption)",
            reply_text,
            source="telegram-photo",
        )

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
