"""Migrations: idempotency, ordering, schema check."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_migrate_creates_all_tables(tmp_path: Path):
    from pelops.migrations import MIGRATIONS, current_version, migrate

    db = tmp_path / "fresh.db"
    applied = migrate(db)

    assert applied == [v for v, _, _ in MIGRATIONS]
    assert current_version(db) == max(v for v, _, _ in MIGRATIONS)

    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "pelops_followups",
        "pelops_watchers",
        "pelops_turns",
        "pelops_tool_calls",
        "pelops_schema",
    }.issubset(tables)
    con.close()


def test_migrate_is_idempotent(tmp_path: Path):
    from pelops.migrations import migrate

    db = tmp_path / "idempotent.db"
    first = migrate(db)
    second = migrate(db)
    assert len(first) > 0
    assert second == []  # nothing new to apply


def test_followups_has_recent_columns(tmp_path: Path):
    from pelops.migrations import migrate

    db = tmp_path / "schema.db"
    migrate(db)
    con = sqlite3.connect(str(db))
    cols = {row[1] for row in con.execute("PRAGMA table_info(pelops_followups)")}
    # Must include the v2 additions
    assert {"attempt_count", "response", "watcher_id"}.issubset(cols)


def test_watchers_has_adaptive_columns(tmp_path: Path):
    from pelops.migrations import migrate

    db = tmp_path / "adapt.db"
    migrate(db)
    con = sqlite3.connect(str(db))
    cols = {row[1] for row in con.execute("PRAGMA table_info(pelops_watchers)")}
    assert {"initial_interval_seconds", "consecutive_noops"}.issubset(cols)


def test_current_version_zero_when_db_missing(tmp_path: Path):
    from pelops.migrations import current_version

    assert current_version(tmp_path / "missing.db") == 0
