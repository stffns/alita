"""Tests for the Deepgram STT module.

Hermetic: `httpx.AsyncClient.post` is patched so we never touch the
Deepgram API. Real-API verification belongs in a separate smoke
script (not committed -- costs credits).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pelops import stt


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
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class _FakeClient:
    """Stand-in for `httpx.AsyncClient` used as an async context manager."""

    def __init__(self, response: _FakeResponse | Exception):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, params=None, headers=None, content=None):
        self.calls.append(
            {"url": url, "params": params, "headers": headers, "content_len": len(content or b"")}
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_client(monkeypatch, response):
    """Replace `httpx.AsyncClient(timeout=...)` with a fake."""
    fake = _FakeClient(response)

    def factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(stt.httpx, "AsyncClient", factory)
    return fake


# ---------- happy path -----------------------------------------------


async def test_transcribe_happy_path(configured, monkeypatch: pytest.MonkeyPatch):
    payload = {
        "metadata": {"request_id": "req-1", "duration": 3.21},
        "results": {
            "channels": [
                {
                    "alternatives": [{"transcript": "Hola Alita, como estas?"}],
                    "detected_language": "es",
                }
            ]
        },
    }
    fake = _patch_client(monkeypatch, _FakeResponse(200, payload))
    result = await stt.transcribe(b"audio-bytes-here", mime_type="audio/ogg")

    assert result["text"] == "Hola Alita, como estas?"
    assert result["language"] == "es"
    assert result["duration_s"] == 3.21
    assert result["model"] == "nova-3"
    assert result["request_id"] == "req-1"

    # Sanity: Authorization header, params, content all wired in.
    call = fake.calls[0]
    assert call["url"] == stt._ENDPOINT
    assert call["headers"]["Authorization"] == "Token dg_test_key"
    assert call["headers"]["Content-Type"] == "audio/ogg"
    assert call["params"]["model"] == "nova-3"
    assert call["params"]["language"] == "multi"
    assert call["params"]["smart_format"] == "true"
    assert call["content_len"] == len(b"audio-bytes-here")


async def test_transcribe_silence_returns_empty_text(configured, monkeypatch: pytest.MonkeyPatch):
    """A 200 OK with no transcript is silence, not an error."""
    payload = {
        "metadata": {"request_id": "req-2", "duration": 1.0},
        "results": {"channels": [{"alternatives": [{"transcript": ""}]}]},
    }
    _patch_client(monkeypatch, _FakeResponse(200, payload))
    result = await stt.transcribe(b"some-bytes", "audio/ogg")
    assert result["text"] == ""
    assert result["language"] is None  # not present in payload


async def test_transcribe_missing_fields_tolerated(configured, monkeypatch: pytest.MonkeyPatch):
    """Empty channels list / missing keys must not raise."""
    _patch_client(monkeypatch, _FakeResponse(200, {}))
    result = await stt.transcribe(b"x", "audio/ogg")
    assert result == {
        "text": "",
        "language": None,
        "duration_s": 0.0,
        "model": "nova-3",
        "request_id": None,
    }


# ---------- configuration / policy ----------------------------------


async def test_transcribe_missing_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Without an API key, transcribe must raise BEFORE hitting Deepgram.

    pydantic-settings reads `env_file=".env"` from the project root,
    so `monkeypatch.delenv` alone is not enough -- the real `.env`
    in the repo still has DEEPGRAM_API_KEY. chdir into an empty
    tmp_path so pydantic finds no .env.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "dummy")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    from pelops.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(stt.SttError, match="DEEPGRAM_API_KEY"):
            await stt.transcribe(b"x", "audio/ogg")
    finally:
        get_settings.cache_clear()


async def test_transcribe_rejects_empty_payload(configured):
    with pytest.raises(stt.SttError, match="empty audio"):
        await stt.transcribe(b"", "audio/ogg")


async def test_transcribe_rejects_oversized_payload(configured, monkeypatch: pytest.MonkeyPatch):
    """Caller flooding us with a 100MB file must be refused upfront."""
    monkeypatch.setattr(stt, "_MAX_AUDIO_BYTES", 10)
    with pytest.raises(stt.SttError, match="too large"):
        await stt.transcribe(b"01234567890123", "audio/ogg")


# ---------- API failure paths ---------------------------------------


async def test_transcribe_http_error_surfaced(configured, monkeypatch: pytest.MonkeyPatch):
    """A 4xx/5xx must raise with the body excerpt for diagnosis."""
    _patch_client(monkeypatch, _FakeResponse(401, text='{"err_code":"INVALID_AUTH"}'))
    with pytest.raises(stt.SttError, match="HTTP 401"):
        await stt.transcribe(b"x", "audio/ogg")


async def test_transcribe_timeout(configured, monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, httpx.TimeoutException("read timeout"))
    with pytest.raises(stt.SttError, match="timed out"):
        await stt.transcribe(b"x", "audio/ogg")


async def test_transcribe_non_json_response(configured, monkeypatch: pytest.MonkeyPatch):
    """A proxy returning HTML 502 must surface a clear SttError, not a
    cryptic JSONDecodeError further down the stack."""
    _patch_client(monkeypatch, _FakeResponse(200, payload=None, text="<html>502</html>"))
    with pytest.raises(stt.SttError, match="not JSON"):
        await stt.transcribe(b"x", "audio/ogg")


# ---------- mime-type pass-through ----------------------------------


async def test_transcribe_passes_mime_type(configured, monkeypatch: pytest.MonkeyPatch):
    """`mime_type` of the audio becomes Content-Type for Deepgram."""
    payload = {
        "metadata": {"duration": 0.1},
        "results": {"channels": [{"alternatives": [{"transcript": "ok"}]}]},
    }
    fake = _patch_client(monkeypatch, _FakeResponse(200, payload))
    await stt.transcribe(b"x", "audio/mpeg")
    assert fake.calls[0]["headers"]["Content-Type"] == "audio/mpeg"


async def test_transcribe_defaults_mime_type_when_blank(
    configured, monkeypatch: pytest.MonkeyPatch
):
    payload = {
        "metadata": {"duration": 0.1},
        "results": {"channels": [{"alternatives": [{"transcript": "ok"}]}]},
    }
    fake = _patch_client(monkeypatch, _FakeResponse(200, payload))
    await stt.transcribe(b"x", "")
    assert fake.calls[0]["headers"]["Content-Type"] == "application/octet-stream"
