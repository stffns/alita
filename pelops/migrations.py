"""Simple forward-only SQL migrations for jobs.db.

Why not Alembic? Pelops has one author, two tables, and never deletes
columns. Hand-rolled migrations stay readable and avoid pulling SQLAlchemy
into the runtime path. Add a new dict entry to MIGRATIONS, bump the
schema_version, restart -- done.

Each migration is an ordered (version, name, sql) tuple. The runner keeps
a single `pelops_schema` table that tracks what has been applied. Idempotent:
applying the same set twice is a no-op.

Replaces the prior `ALTER TABLE ... try/except sqlite3.OperationalError`
pattern that worked but quietly swallowed real bugs.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("pelops.migrations")


# (version, label, sql).  KEEP ORDERED. Never reorder; never delete.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "create followups",
        """
        CREATE TABLE IF NOT EXISTS pelops_followups (
            id            TEXT PRIMARY KEY,
            label         TEXT,
            prompt        TEXT NOT NULL,
            run_at_utc    TEXT NOT NULL,
            cron          TEXT,
            created_at    TEXT NOT NULL,
            claimed_at    TEXT,
            fired_at      TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            last_error    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pelops_followups_due
            ON pelops_followups(status, run_at_utc);
        """,
    ),
    (
        2,
        "followups: retry + response + watcher_id",
        """
        ALTER TABLE pelops_followups ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE pelops_followups ADD COLUMN response TEXT;
        ALTER TABLE pelops_followups ADD COLUMN watcher_id TEXT;
        """,
    ),
    (
        3,
        "create watchers",
        """
        CREATE TABLE IF NOT EXISTS pelops_watchers (
            id                       TEXT PRIMARY KEY,
            label                    TEXT,
            query                    TEXT NOT NULL,
            interval_seconds         INTEGER NOT NULL,
            initial_interval_seconds INTEGER,
            consecutive_noops        INTEGER NOT NULL DEFAULT 0,
            last_checked_at          TEXT,
            last_seen_hash           TEXT,
            last_seen_response       TEXT,
            active                   INTEGER NOT NULL DEFAULT 1,
            created_at               TEXT NOT NULL,
            last_alert_at            TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pelops_watchers_due
            ON pelops_watchers(active, last_checked_at);
        """,
    ),
    (
        4,
        "create metrics tables",
        """
        CREATE TABLE IF NOT EXISTS pelops_turns (
            id                INTEGER PRIMARY KEY,
            timestamp         TEXT NOT NULL,
            model             TEXT,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            duration_ms       INTEGER,
            source            TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_ts ON pelops_turns(timestamp);
        CREATE TABLE IF NOT EXISTS pelops_tool_calls (
            id          INTEGER PRIMARY KEY,
            timestamp   TEXT NOT NULL,
            name        TEXT NOT NULL,
            duration_ms INTEGER,
            ok          INTEGER NOT NULL DEFAULT 1,
            error       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tools_ts ON pelops_tool_calls(timestamp, name);
        """,
    ),
]


def _ensure_meta_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pelops_schema (
            version    INTEGER PRIMARY KEY,
            label      TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _applied_versions(con: sqlite3.Connection) -> set[int]:
    _ensure_meta_table(con)
    rows = con.execute("SELECT version FROM pelops_schema").fetchall()
    return {r[0] for r in rows}


def _execute_tolerant(con: sqlite3.Connection, sql: str) -> None:
    """Run a multi-statement SQL script tolerating two predictable errors:

    - "duplicate column name X" -- the column already exists because an
      earlier (pre-migrations) build of Pelops already added it via the
      legacy try/except ALTER pattern.
    - "table X already exists" -- shouldn't happen with IF NOT EXISTS but
      defensive.

    Any other OperationalError is re-raised.
    """
    # Split on semicolons that end a statement. Naive but adequate for our
    # DDL-only migrations (no embedded semicolons in strings).
    for stmt in [s.strip() for s in sql.strip().split(";") if s.strip()]:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                continue
            raise


def migrate(db_path: str | Path) -> list[int]:
    """Apply all pending migrations to the given DB. Returns the list of
    versions newly applied (empty when the DB was already up to date).

    Tolerant of DBs that pre-date this module: migrations whose target
    columns / tables already exist will record themselves as applied
    without re-running the DDL.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        applied = _applied_versions(con)
        newly: list[int] = []
        for version, label, sql in MIGRATIONS:
            if version in applied:
                continue
            log.info("applying migration %d: %s", version, label)
            _execute_tolerant(con, sql)
            con.execute(
                "INSERT INTO pelops_schema (version, label) VALUES (?, ?)",
                (version, label),
            )
            newly.append(version)
        return newly
    finally:
        con.close()


def current_version(db_path: str | Path) -> int:
    """Highest applied migration version, or 0 if none."""
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        _ensure_meta_table(con)
        row = con.execute("SELECT MAX(version) FROM pelops_schema").fetchone()
        return int(row[0] or 0)
    finally:
        con.close()
