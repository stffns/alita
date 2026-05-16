"""Shared pytest fixtures.

The fixtures here let us exercise the storage modules (jobs, watchers,
metrics) against a throwaway SQLite without touching the real `data/`
directory the agent uses in development.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point PELOPS_VSTASH_DB at a temp file and reset the Settings cache.

    Each test that touches jobs.py / watchers.py / metrics.py gets a clean
    database. The fixture also flushes pelops.config's lru_cache so the
    new env var is actually picked up.
    """
    db = tmp_path / "test.db"
    # GROQ_API_KEY is required by Settings; provide a dummy so unit tests
    # never need a real key just to load config.
    monkeypatch.setenv("GROQ_API_KEY", "test-dummy-key")
    monkeypatch.setenv("PELOPS_VSTASH_DB", str(db))
    # Settings is cached; flush so the new env is honored.
    try:
        from pelops.config import get_settings

        get_settings.cache_clear()
    except ImportError:
        pass
    yield db
