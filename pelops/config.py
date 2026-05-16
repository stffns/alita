"""Runtime configuration via pydantic-settings.

Settings are loaded from environment variables (and the local `.env` file
when present). Validation runs at import time, so a typo or missing key
fails fast with a clear error instead of a downstream NoneType crash.

Secrets (API keys, bot tokens) are stored as `pydantic.SecretStr` -- they
do NOT print themselves when the model is repr'd or logged. Use
`.get_secret_value()` to access the raw string when calling an API.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Pelops runtime configuration.

    Every field declares the env var name explicitly via `alias=` so the
    `.env` and the system shell stay readable. Pydantic enforces types
    at load time: a missing required key or a bad value raises a clear
    `ValidationError` instead of crashing later in unrelated code.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ---- LLM providers ------------------------------------------------ #
    groq_api_key: SecretStr = Field(alias="GROQ_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")

    chat_model: str = Field(
        default="openrouter:deepseek/deepseek-v4-flash",
        alias="PELOPS_CHAT_MODEL",
    )
    research_model: str = Field(default="groq/compound", alias="PELOPS_RESEARCH_MODEL")
    research_model_fast: str = Field(
        default="groq/compound-mini",
        alias="PELOPS_RESEARCH_MODEL_FAST",
    )

    # ---- Memory (vstash) --------------------------------------------- #
    vstash_project: str = Field(default="pelops", alias="PELOPS_VSTASH_PROJECT")
    vstash_db: Path = Field(default=Path("./data/pelops.db"), alias="PELOPS_VSTASH_DB")

    # ---- Wiki (compiled-knowledge layer) ----------------------------- #
    # Markdown vault with YAML frontmatter and [[backlinks]]. Obsidian-
    # compatible. See docs/wiki-plan.md.
    wiki_dir: Path = Field(
        default=Path.home() / "Documents" / "pelops-wiki" / "Alita",
        alias="PELOPS_WIKI_DIR",
    )

    # ---- Identity / focus -------------------------------------------- #
    # NoDecode skips pydantic-settings' default JSON parsing so our
    # CSV-style env values ("AI agents,LLM tooling") flow straight to the
    # field_validator below.
    owner: str = Field(default="friend", alias="PELOPS_OWNER")
    topics: Annotated[list[str], NoDecode] = Field(default_factory=list, alias="PELOPS_TOPICS")
    rss_feeds: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="PELOPS_RSS_FEEDS"
    )

    # ---- Scheduler crons --------------------------------------------- #
    briefing_cron: str = Field(default="0 8 * * *", alias="PELOPS_BRIEFING_CRON")
    consolidate_cron: str = Field(default="0 3 * * *", alias="PELOPS_CONSOLIDATE_CRON")
    ingest_cron: str = Field(default="0 */6 * * *", alias="PELOPS_INGEST_CRON")

    # ---- Telegram transport ------------------------------------------ #
    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_owner_chat_id: int | None = Field(default=None, alias="TELEGRAM_OWNER_CHAT_ID")

    # ---------- validators ---------- #

    @field_validator("topics", "rss_feeds", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v or []

    @field_validator("telegram_owner_chat_id", mode="before")
    @classmethod
    def _parse_chat_id(cls, v):
        """Tolerate common typos when copying the chat id from Telegram:
        leading '=' or '+', surrounding whitespace. Returns None on
        anything that does not look like an integer."""
        if v is None or v == "":
            return None
        if isinstance(v, str):
            cleaned = v.strip().lstrip("=+ ")
            if not cleaned:
                return None
            try:
                return int(cleaned)
            except ValueError:
                return None
        return v

    @field_validator("vstash_db", mode="after")
    @classmethod
    def _ensure_db_parent(cls, v: Path) -> Path:
        absolute = v.expanduser().resolve()
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return absolute

    # ---------- back-compat helper ---------- #

    @classmethod
    def load(cls) -> Settings:
        """Legacy entry point used across the codebase.

        Equivalent to `get_settings()`. Kept so the call sites
        (`Settings.load()`) do not all need to change at once.
        """
        return get_settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton settings instance.

    Cached for the lifetime of the process. To pick up a changed `.env`
    you must restart the process (this matches how the bot and the
    schedulers run).
    """
    return Settings()  # type: ignore[call-arg]
