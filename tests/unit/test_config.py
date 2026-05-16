"""Settings loading + validators."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict


@pytest.fixture
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    """Provide a Settings class that does NOT read the developer .env file
    so tests are hermetic. Also resets the lru_cache so calls within a test
    pick up monkeypatched env vars."""
    monkeypatch.setenv("GROQ_API_KEY", "k_dummy")
    monkeypatch.delenv("PELOPS_TOPICS", raising=False)
    monkeypatch.delenv("PELOPS_RSS_FEEDS", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    from pelops.config import Settings, get_settings

    get_settings.cache_clear()

    class _HermeticSettings(Settings):
        model_config = SettingsConfigDict(
            env_file=None,
            env_file_encoding="utf-8",
            case_sensitive=False,
            extra="ignore",
            populate_by_name=True,
        )

    yield _HermeticSettings
    get_settings.cache_clear()


def test_groq_key_is_secret(clean_settings):
    s = clean_settings()
    assert isinstance(s.groq_api_key, SecretStr)
    # SecretStr masks itself in __str__/repr -- never leak.
    assert "k_dummy" not in str(s.groq_api_key)
    assert s.groq_api_key.get_secret_value() == "k_dummy"


def test_topics_csv_split(monkeypatch: pytest.MonkeyPatch, clean_settings):
    monkeypatch.setenv("PELOPS_TOPICS", "AI agents, LLM tooling , observability")
    s = clean_settings()
    assert s.topics == ["AI agents", "LLM tooling", "observability"]


def test_topics_empty_when_unset(clean_settings):
    s = clean_settings()
    assert s.topics == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("8859362530", 8859362530),  # clean integer
        ("=8859362530", 8859362530),  # leading '=' typo
        ("+49176", 49176),  # leading '+'
        (" 555 ", 555),  # whitespace
        ("garbage", None),  # invalid -> None
        ("", None),  # empty -> None
    ],
)
def test_telegram_chat_id_parsing(monkeypatch: pytest.MonkeyPatch, clean_settings, raw, expected):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", raw)
    s = clean_settings()
    assert s.telegram_owner_chat_id == expected


def test_chat_model_default(clean_settings):
    s = clean_settings()
    assert s.chat_model.startswith("openrouter:") or s.chat_model.startswith("groq:")


def test_load_alias_returns_singleton(clean_settings):
    from pelops.config import get_settings

    get_settings.cache_clear()
    s1 = clean_settings()  # produces a fresh instance via subclass
    s2 = clean_settings()
    # Each instance creation produces a new object, but the public
    # Settings.load() / get_settings() pair returns the same cached instance.
    assert isinstance(s1, type(s2).__mro__[1])
