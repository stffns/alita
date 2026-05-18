"""Tests for the Deepgram Aura TTS module.

Hermetic: `httpx.AsyncClient.post` is patched -- no real Aura calls
(they cost). Real-API smoke belongs in an uncommitted script.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pelops import tts


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    """Ensure DEEPGRAM_API_KEY is set and Settings cache is fresh."""
    monkeypatch.setenv("GROQ_API_KEY", "dummy")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg_test_key")
    from pelops.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text or (content.decode("latin-1", errors="replace") if content else "")


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, params=None, headers=None, json=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "json": json})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_client(monkeypatch, response):
    fake = _FakeClient(response)

    def factory(*args: Any, **kwargs: Any):
        return fake

    monkeypatch.setattr(tts.httpx, "AsyncClient", factory)
    return fake


# ---------- markdown stripping ----------------------------------------


def test_strip_markdown_removes_fenced_code():
    out = tts._strip_markdown("Hola\n```python\nprint(1)\n```\nadios")
    assert "```" not in out
    assert "print(1)" not in out
    assert "codigo omitido" in out


def test_strip_markdown_keeps_link_label_drops_url():
    out = tts._strip_markdown("Mira [esto](https://example.com) por favor")
    assert "esto" in out
    assert "example.com" not in out


def test_strip_markdown_handles_bold_italic_inline_code_headings():
    src = "## Titulo\n**bold** y *italic* y `code`\n- bullet 1\n- bullet 2"
    out = tts._strip_markdown(src)
    assert "##" not in out
    assert "**" not in out and "*italic*" not in out
    assert "`" not in out
    assert "bullet 1" in out and "bullet 2" in out
    # Bullets become commas so the rhythm sounds natural.
    assert "," in out


# ---------- length trimming -------------------------------------------


def test_trim_snaps_to_sentence_boundary():
    text = "Primera frase. Segunda frase. Tercera frase. Cuarta frase."
    trimmed = tts._trim_for_speech(text, max_chars=35)
    assert trimmed.endswith(".")
    assert "Primera frase." in trimmed
    assert "Tercera frase" not in trimmed


def test_trim_falls_back_to_word_boundary_when_no_sentence_end():
    text = "palabra una palabra dos palabra tres palabra cuatro"
    trimmed = tts._trim_for_speech(text, max_chars=20)
    # Must not chop a word in half.
    assert not trimmed.endswith("pal") and not trimmed.endswith("palabr")
    assert len(trimmed) <= 20


def test_trim_short_passthrough():
    assert tts._trim_for_speech("hola", 600) == "hola"


def test_prepare_speech_text_strips_and_trims():
    """The public helper composes strip + trim."""
    src = "## Header\n" + "Frase corta. " * 100
    out = tts.prepare_speech_text(src, max_chars=80)
    assert "##" not in out
    assert len(out) <= 80
    assert out.endswith(".")


# ---------- HTTP happy path -------------------------------------------


async def test_synthesize_happy_path(configured, monkeypatch: pytest.MonkeyPatch):
    fake = _patch_client(monkeypatch, _FakeResponse(200, content=b"OggS-audio-bytes"))
    audio = await tts.synthesize("Hola Jay, te respondo en voz.")

    assert audio == b"OggS-audio-bytes"
    call = fake.calls[0]
    assert call["url"] == tts._ENDPOINT
    assert call["headers"]["Authorization"] == "Token dg_test_key"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["params"]["encoding"] == "opus"
    assert call["params"]["container"] == "ogg"
    # sample_rate intentionally absent: Aura rejects it with opus
    # (HTTP 400 UNSUPPORTED_AUDIO_FORMAT). Opus is always 48kHz.
    assert "sample_rate" not in call["params"]
    assert call["params"]["model"] == "aura-2-celeste-es"  # default
    # The JSON body's text is markdown-stripped + trimmed -- here it's
    # short so it passes through unchanged.
    assert call["json"] == {"text": "Hola Jay, te respondo en voz."}


async def test_synthesize_respects_voice_override(configured, monkeypatch: pytest.MonkeyPatch):
    fake = _patch_client(monkeypatch, _FakeResponse(200, content=b"x"))
    await tts.synthesize("hola", voice="aura-asteria-en")
    assert fake.calls[0]["params"]["model"] == "aura-asteria-en"


async def test_synthesize_strips_markdown_before_sending(
    configured, monkeypatch: pytest.MonkeyPatch
):
    fake = _patch_client(monkeypatch, _FakeResponse(200, content=b"x"))
    await tts.synthesize("**negrita** y `codigo` mezclados")
    body_text = fake.calls[0]["json"]["text"]
    assert "**" not in body_text
    assert "`" not in body_text
    assert "negrita" in body_text and "codigo" in body_text


async def test_synthesize_trims_long_input(configured, monkeypatch: pytest.MonkeyPatch):
    """Long replies must be capped before hitting Aura."""
    fake = _patch_client(monkeypatch, _FakeResponse(200, content=b"x"))
    long_text = "Frase de cinco palabras aqui. " * 200  # ~6000 chars
    await tts.synthesize(long_text)
    sent = fake.calls[0]["json"]["text"]
    assert len(sent) <= tts._SPEECH_CHAR_CAP


# ---------- error paths -----------------------------------------------


async def test_synthesize_missing_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Must raise BEFORE hitting Aura when no key is configured.

    pydantic-settings reads `env_file=".env"` from the project root,
    so `monkeypatch.delenv` alone is not enough -- the real `.env`
    in the repo still has DEEPGRAM_API_KEY. chdir into an empty
    tmp_path so pydantic finds no .env at all.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "dummy")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    from pelops.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(tts.TtsError, match="DEEPGRAM_API_KEY"):
            await tts.synthesize("hola")
    finally:
        get_settings.cache_clear()


async def test_synthesize_rejects_empty_after_trim(configured, monkeypatch: pytest.MonkeyPatch):
    """Input that strips to nothing must NOT hit Aura.

    Pure whitespace collapses to "" via `_strip_markdown`'s final
    whitespace-normalize step. Fenced code is replaced with "(codigo
    omitido)" so that input is NOT empty -- we test whitespace-only
    instead, which is the realistic edge case (the agent could return
    `"  \n\n  "` from a broken tool call).
    """
    _patch_client(monkeypatch, _FakeResponse(200, content=b"never-called"))
    with pytest.raises(tts.TtsError, match="empty text"):
        await tts.synthesize("   \n\n   ")


async def test_synthesize_http_error_surfaced(configured, monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, _FakeResponse(403, text='{"err":"bad-voice-id"}'))
    with pytest.raises(tts.TtsError, match="HTTP 403"):
        await tts.synthesize("hola")


async def test_synthesize_timeout(configured, monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, httpx.TimeoutException("synth timeout"))
    with pytest.raises(tts.TtsError, match="timed out"):
        await tts.synthesize("hola")


async def test_synthesize_empty_audio_response(configured, monkeypatch: pytest.MonkeyPatch):
    """A 200 OK with no body must NOT silently return empty bytes."""
    _patch_client(monkeypatch, _FakeResponse(200, content=b""))
    with pytest.raises(tts.TtsError, match="empty audio body"):
        await tts.synthesize("hola")
