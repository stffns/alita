"""Text-to-speech via Deepgram Aura.

Symmetric counterpart to `pelops/stt.py`. When Jay sends a voice note,
Alita transcribes it (STT) AND replies with synthesized speech (TTS)
so the round-trip stays in the same modality. Reasoning: voice notes
get sent while walking / cooking / driving, situations where text
replies are inconvenient.

The cap on input length is deliberate: Alita's text replies tend to
be dense and analytical (lists, code, multiple paragraphs). That does
not translate well to spoken audio. We trim the reply to roughly the
first 600 characters of plain prose -- usually the first 2-3
sentences, which covers the gist -- and let Jay read the full text
in the parallel chat message.

Public surface:
    async synthesize(text: str, voice: str | None = None) -> bytes

Returns: OGG/Opus audio bytes ready for `bot.send_voice` on Telegram.

Raises `TtsError` on config / API / size issues so the caller can
recover (typically: log + continue without voice, since the text
message is already sent).
"""

from __future__ import annotations

import logging
import re

import httpx

from pelops.config import Settings

_log = logging.getLogger("pelops.tts")

_ENDPOINT = "https://api.deepgram.com/v1/speak"

# Hard cap on text we send to Aura. The API itself accepts up to
# ~2000 chars per request but we want shorter for audio UX: ~600 is
# about 30 seconds of speech at normal pace, which is the upper
# bound of "voice note someone will actually listen to". The trim
# tries to land on a sentence boundary; see `_trim_for_speech`.
_SPEECH_CHAR_CAP = 600

# Aura asks for opus + ogg container if you want a stream Telegram
# can render as a voice note (Opus codec is required for `send_voice`).
# NOTE: `sample_rate` is NOT applicable to opus -- Deepgram returns
# HTTP 400 UNSUPPORTED_AUDIO_FORMAT if you try to pass it. Opus is
# always 48kHz under the hood; the param would be redundant anyway.
_AURA_PARAMS = {
    "encoding": "opus",
    "container": "ogg",
}

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class TtsError(RuntimeError):
    """Raised when synthesis cannot proceed (config / size / API)."""


# --------------------------------------------------------------------- #
# Text preparation
# --------------------------------------------------------------------- #

# Markdown noise that sounds awful when read aloud. Strip in order:
# fenced code blocks first (Aura would read every backtick), then
# inline code, then bold/italic/heading markers. Bullet markers are
# replaced with a comma so the rhythm sounds like a list.
_FENCED_CODE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD_ITALIC = re.compile(r"(\*\*|__|\*|_)([^*_]+)\1")
_HEADINGS = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BULLETS = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_LINKS = re.compile(r"\[([^\]]+)\]\([^)]+\)")  # keep label, drop URL


def _strip_markdown(text: str) -> str:
    """Make `text` sound natural when read aloud.

    The intent is NOT to perfectly render markdown -- it is to remove
    the symbols that Aura would otherwise pronounce as "asterisk",
    "underscore", "backtick", etc.
    """
    text = _FENCED_CODE.sub(" (codigo omitido) ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD_ITALIC.sub(r"\2", text)
    text = _HEADINGS.sub("", text)
    text = _BULLETS.sub(", ", text)
    text = _LINKS.sub(r"\1", text)
    # Collapse runs of whitespace introduced by the substitutions.
    return re.sub(r"\s+", " ", text).strip()


# Sentence-ending punctuation we are willing to truncate on. Order
# matters: prefer the LATEST boundary inside the cap.
_SENTENCE_END = re.compile(r"[.!?]\s+")


def _trim_for_speech(text: str, max_chars: int) -> str:
    """Return at most `max_chars` of text, snapped to a sentence end if possible.

    If the cap lands inside a sentence we look back for the most
    recent sentence-ending punctuation; if none exists within the cap
    we settle for a word boundary.
    """
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    matches = list(_SENTENCE_END.finditer(window))
    if matches:
        return window[: matches[-1].end()].rstrip()
    # No sentence end found: fall back to last whitespace inside the
    # window so we never cut a word in half.
    last_space = window.rfind(" ")
    return window[: last_space if last_space > 0 else max_chars].rstrip()


def prepare_speech_text(text: str, max_chars: int = _SPEECH_CHAR_CAP) -> str:
    """Public helper: strip markdown + trim to spoken-friendly length.

    Exposed because the Telegram handler needs the SAME trimmed text
    we are about to synthesize (so the chat message can say "leyendo
    los primeros N caracteres ..." or similar if we ever surface it).
    """
    return _trim_for_speech(_strip_markdown(text), max_chars)


# --------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------- #


async def synthesize(text: str, voice: str | None = None) -> bytes:
    """Synthesize `text` to OGG/Opus audio via Deepgram Aura.

    Args:
        text: plain text to read aloud. Will be markdown-stripped and
            trimmed to `_SPEECH_CHAR_CAP` (~600 chars) before sending
            to Aura -- the caller does NOT need to pre-process.
        voice: Aura voice id (e.g. `aura-2-celeste-es`). Defaults to
            `Settings.deepgram_tts_voice`.

    Returns:
        OGG/Opus audio bytes ready for `bot.send_voice`.

    Raises:
        TtsError: missing key, empty text after trimming, HTTP error,
            malformed response.
    """
    s = Settings.load()
    if s.deepgram_api_key is None:
        raise TtsError("DEEPGRAM_API_KEY not configured. Add it to .env to enable voice replies.")
    spoken = prepare_speech_text(text)
    if not spoken:
        raise TtsError("empty text after trimming -- nothing to synthesize")

    voice_id = voice or s.deepgram_tts_voice
    params = {"model": voice_id, **_AURA_PARAMS}
    headers = {
        "Authorization": f"Token {s.deepgram_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    body = {"text": spoken}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(_ENDPOINT, params=params, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise TtsError(f"Deepgram TTS timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise TtsError(f"Deepgram TTS request failed: {exc}") from exc

    if r.status_code >= 400:
        # Aura returns JSON errors; surface enough to debug a wrong
        # voice id or quota issue without leaking the auth header.
        raise TtsError(f"Deepgram TTS returned HTTP {r.status_code}: {r.text[:200]!r}")

    audio = r.content
    if not audio:
        raise TtsError("Deepgram TTS returned empty audio body")
    _log.info(
        "tts: synthesized %d chars -> %d bytes (voice=%s)",
        len(spoken),
        len(audio),
        voice_id,
    )
    return audio
