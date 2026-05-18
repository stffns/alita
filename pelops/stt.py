"""Speech-to-text via Deepgram for Telegram voice notes.

Jay records voice notes on his phone while walking / cooking; the bot
transcribes them through Deepgram and the resulting text flows into
the agent like any chat message.

Why Deepgram and not OpenAI Whisper / OpenRouter / a local model:
  - Jay already has a Deepgram account.
  - The REST API is one HTTP call -- no SDK install, no streaming
    plumbing, no model weights to ship.
  - `nova-3` + `language=multi` handles Jay's Spanish/English mix
    automatically; no language-detection step on our side.

Public surface:
    async transcribe(audio_bytes: bytes, mime_type: str) -> dict

Returns:
    {
        "text": str,            # the transcript ("" if no speech)
        "language": str | None, # auto-detected ISO code, if any
        "duration_s": float,    # length of the audio, seconds
        "model": str,           # the Deepgram model that ran
        "request_id": str | None,
    }

Raises `SttError` on policy / config / API failures so the caller
(the Telegram bot) can render a friendly reply instead of crashing.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pelops.config import Settings

_log = logging.getLogger("pelops.stt")

_ENDPOINT = "https://api.deepgram.com/v1/listen"
# Maximum audio size we accept in a single request. Telegram voice
# notes top out around 1MB for a few-minute clip, so this is generous.
# Past this we refuse rather than load a giant blob into RAM and
# burn Deepgram credits on a likely-bogus file.
_MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB

# Deepgram's connect/read timeout split. Voice notes are short; if
# transcription takes more than 30s something is wrong.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


class SttError(RuntimeError):
    """Raised when transcription cannot proceed (config, size, API)."""


async def transcribe(audio_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """Transcribe `audio_bytes` via Deepgram.

    Args:
        audio_bytes: raw audio payload as Telegram delivers it.
        mime_type: e.g. `audio/ogg` for a voice note, `audio/mpeg`
            for a forwarded MP3. Deepgram sniffs the codec from the
            bytes, but passing the right Content-Type accelerates
            the path.

    Returns:
        The shape documented in the module docstring.

    Raises:
        SttError: missing API key, oversized payload, HTTP error,
            malformed Deepgram response.
    """
    s = Settings.load()
    if s.deepgram_api_key is None:
        raise SttError(
            "DEEPGRAM_API_KEY not configured. Add it to .env to enable voice-note transcription."
        )
    if not audio_bytes:
        raise SttError("empty audio payload")
    if len(audio_bytes) > _MAX_AUDIO_BYTES:
        raise SttError(f"audio too large: {len(audio_bytes):,} bytes (cap {_MAX_AUDIO_BYTES:,}).")

    params = {
        "model": s.deepgram_model,
        "language": s.deepgram_language,
        "smart_format": "true",  # capitalization, punctuation
        "punctuate": "true",  # explicit even though smart_format implies it
    }
    headers = {
        "Authorization": f"Token {s.deepgram_api_key.get_secret_value()}",
        "Content-Type": mime_type or "application/octet-stream",
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(_ENDPOINT, params=params, headers=headers, content=audio_bytes)
    except httpx.TimeoutException as exc:
        raise SttError(f"Deepgram timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise SttError(f"Deepgram request failed: {exc}") from exc

    if r.status_code >= 400:
        # Deepgram returns JSON errors with `err_code` / `err_msg`; if
        # the body is something else (HTML 502 page from a proxy, etc),
        # surface the first 200 chars so the caller can see what went
        # wrong without us guessing.
        raise SttError(f"Deepgram returned HTTP {r.status_code}: {r.text[:200]!r}")

    try:
        data = r.json()
    except ValueError as exc:
        raise SttError(f"Deepgram response was not JSON: {r.text[:200]!r}") from exc

    return _extract(data, model=s.deepgram_model)


def _extract(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Pull the fields we care about out of Deepgram's nested response.

    Deepgram's payload shape (trimmed):

        {
          "metadata": {
            "request_id": "...",
            "duration": 4.32,
            "model_info": {...}
          },
          "results": {
            "channels": [
              {
                "alternatives": [
                  {"transcript": "...", "confidence": 0.99}
                ],
                "detected_language": "es"
              }
            ]
          }
        }

    We tolerate every part being missing -- a 200 OK with an empty
    transcript is still a valid "we heard silence" result, not an
    error.
    """
    metadata = data.get("metadata") or {}
    results = data.get("results") or {}
    channels = results.get("channels") or []
    channel = channels[0] if channels else {}
    alternatives = channel.get("alternatives") or []
    best = alternatives[0] if alternatives else {}

    return {
        "text": (best.get("transcript") or "").strip(),
        "language": channel.get("detected_language"),
        "duration_s": float(metadata.get("duration") or 0.0),
        "model": model,
        "request_id": metadata.get("request_id"),
    }
