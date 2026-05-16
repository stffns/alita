"""Watchers: change-driven (not time-driven) triggers for Pelops.

A watcher is a saved research query that the scheduler re-runs on its
interval. If the response is meaningfully different from the last time,
Pelops fires an autonomous follow-up to alert the owner. If nothing
changed, the watcher stays quiet.

This is what makes Pelops feel reactive to the world rather than to the
clock: pushes happen when something *happens*, not when it's 9am.

Schema:

    pelops_watchers
        id                 TEXT PRIMARY KEY        'w_<uuid8>_<label>'
        label              TEXT                    short slug
        query              TEXT NOT NULL           research query
        interval_seconds   INTEGER NOT NULL        re-check cadence
        last_checked_at    TEXT                    ISO UTC
        last_seen_hash     TEXT                    sha256 of normalized response
        last_seen_response TEXT                    full response for diff context
        active             INTEGER NOT NULL        0/1
        created_at         TEXT NOT NULL
        last_alert_at      TEXT                    when we last pushed an alert
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pelops.config import Settings

log = logging.getLogger("pelops.watchers")

_WATCHER_PREFIX = "w_"

# Adaptive-cadence tuning.
NOOPS_BEFORE_BACKOFF = 5  # consecutive NOOPs needed to double interval
MAX_INTERVAL_SECONDS = 86_400  # 1 day cap
BACKOFF_MULTIPLIER = 2
SPEEDUP_DIVISOR = 2  # on alert after backoff, halve toward initial


def _db_path() -> Path:
    s = Settings.load()
    return Path(s.vstash_db).parent / "jobs.db"


def _connect() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), isolation_level=None, timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute(
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
        )
        """
    )
    # Migrate older rows.
    for ddl in (
        "ALTER TABLE pelops_watchers ADD COLUMN initial_interval_seconds INTEGER",
        "ALTER TABLE pelops_watchers ADD COLUMN consecutive_noops INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    con.execute(
        "UPDATE pelops_watchers "
        "SET initial_interval_seconds = interval_seconds "
        "WHERE initial_interval_seconds IS NULL"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_pelops_watchers_due "
        "ON pelops_watchers(active, last_checked_at)"
    )
    return con


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(text: str) -> str:
    """Strip dates/timestamps so 'as of 2026-05-14 16:00' vs '2026-05-14 18:00'
    don't register as a real change. Keep the substance."""
    if not text:
        return ""
    s = text.lower()
    # ISO and slash date forms
    s = re.sub(r"\d{4}[-/]\d{2}[-/]\d{2}([t ]\d{2}:\d{2}(:\d{2})?)?", "<date>", s)
    # human times
    s = re.sub(r"\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?", "<time>", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


_INTERVAL_RE = re.compile(
    r"^\s*every\s+(\d+)\s+"
    r"(second|seconds|minute|minutes|hour|hours|day|days)\s*$",
    re.IGNORECASE,
)
_INTERVAL_TO_SECONDS = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
}


def parse_interval(interval: str | int) -> int:
    """Accept either an integer of seconds or a phrase like 'every 2 hours'."""
    if isinstance(interval, int):
        return max(60, interval)  # minimum 1 min to avoid runaways
    s = str(interval).strip()
    if s.isdigit():
        return max(60, int(s))
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(
            f"Cannot parse interval={interval!r}. Use 'every N minutes/hours/days' "
            f"or an integer of seconds."
        )
    n = int(m.group(1))
    unit_s = _INTERVAL_TO_SECONDS[m.group(2).lower()]
    return max(60, n * unit_s)


def add(query: str, interval: str | int, label: str | None = None) -> str:
    label = (label or "watch").strip().replace(" ", "-")[:40]
    wid = f"{_WATCHER_PREFIX}{uuid.uuid4().hex[:8]}_{label}"
    secs = parse_interval(interval)
    with _connect() as con:
        con.execute(
            "INSERT INTO pelops_watchers "
            "(id, label, query, interval_seconds, initial_interval_seconds, "
            " created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (wid, label, query, secs, secs, _now_iso()),
        )
    log.info("added watcher %s every %ds query=%r", wid, secs, query[:80])
    return wid


def note_noop(watcher_id: str) -> dict:
    """Increment NOOP counter. If we hit the threshold, back off (double the
    interval). Returns a dict describing the state change so the caller can
    surface a meta-message to the owner if interval changed.
    """
    with _connect() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT interval_seconds, consecutive_noops FROM pelops_watchers WHERE id = ?",
            (watcher_id,),
        ).fetchone()
        if not row:
            return {"changed": False}
        new_noops = (row["consecutive_noops"] or 0) + 1
        current_interval = row["interval_seconds"]
        if new_noops < NOOPS_BEFORE_BACKOFF or current_interval >= MAX_INTERVAL_SECONDS:
            con.execute(
                "UPDATE pelops_watchers SET consecutive_noops = ? WHERE id = ?",
                (new_noops, watcher_id),
            )
            return {"changed": False, "noops": new_noops}
        new_interval = min(current_interval * BACKOFF_MULTIPLIER, MAX_INTERVAL_SECONDS)
        con.execute(
            "UPDATE pelops_watchers SET interval_seconds = ?, consecutive_noops = 0 WHERE id = ?",
            (new_interval, watcher_id),
        )
        log.info(
            "watcher %s: %d NOOPs -> backing off %ds -> %ds",
            watcher_id,
            new_noops,
            current_interval,
            new_interval,
        )
        return {
            "changed": True,
            "old_interval": current_interval,
            "new_interval": new_interval,
            "direction": "slower",
        }


def note_alert(watcher_id: str) -> dict:
    """A real alert just fired. Reset NOOP counter and, if we had previously
    backed off, gently speed up (halve the interval toward initial).
    """
    with _connect() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT interval_seconds, initial_interval_seconds, consecutive_noops "
            "FROM pelops_watchers WHERE id = ?",
            (watcher_id,),
        ).fetchone()
        if not row:
            return {"changed": False}
        current = row["interval_seconds"]
        initial = row["initial_interval_seconds"] or current
        if current <= initial:
            # Already at or tighter than initial; just reset counter.
            con.execute(
                "UPDATE pelops_watchers SET consecutive_noops = 0 WHERE id = ?",
                (watcher_id,),
            )
            return {"changed": False}
        new_interval = max(current // SPEEDUP_DIVISOR, initial)
        con.execute(
            "UPDATE pelops_watchers SET interval_seconds = ?, consecutive_noops = 0 WHERE id = ?",
            (new_interval, watcher_id),
        )
        log.info(
            "watcher %s: real change after quiet period -> tightening %ds -> %ds",
            watcher_id,
            current,
            new_interval,
        )
        return {
            "changed": True,
            "old_interval": current,
            "new_interval": new_interval,
            "direction": "faster",
        }


def _fmt_interval(seconds: int) -> str:
    if seconds >= 86_400 and seconds % 86_400 == 0:
        return f"every {seconds // 86_400} day{'s' if seconds // 86_400 != 1 else ''}"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"every {seconds // 3600} hour{'s' if seconds // 3600 != 1 else ''}"
    if seconds >= 60 and seconds % 60 == 0:
        return f"every {seconds // 60} minute{'s' if seconds // 60 != 1 else ''}"
    return f"every {seconds} seconds"


def cancel(watcher_id: str) -> bool:
    with _connect() as con:
        cur = con.execute(
            "UPDATE pelops_watchers SET active = 0 WHERE id = ? AND active = 1",
            (watcher_id,),
        )
        return cur.rowcount > 0


def list_active() -> list[dict]:
    with _connect() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, label, query, interval_seconds, last_checked_at, last_alert_at "
            "FROM pelops_watchers WHERE active = 1 ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def claim_due() -> list[dict]:
    """Return watchers due for a re-check (last_checked_at + interval <= now).

    NOT a true atomic claim here because the watcher poller runs in one process
    only (the bot). If we ever scale to multiple pollers we'd add a similar
    UPDATE-and-read pattern as in jobs.claim_due.
    """
    now = datetime.now(UTC)
    with _connect() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, label, query, interval_seconds, last_checked_at, "
            "       last_seen_hash, last_seen_response "
            "FROM pelops_watchers WHERE active = 1"
        ).fetchall()
    due = []
    for r in rows:
        last = r["last_checked_at"]
        if last is None:
            due.append(dict(r))
            continue
        last_dt = datetime.fromisoformat(last)
        if (now - last_dt).total_seconds() >= r["interval_seconds"]:
            due.append(dict(r))
    return due


def update_state(watcher_id: str, new_response: str, *, alerted: bool) -> None:
    new_hash = _hash(new_response)
    now = _now_iso()
    with _connect() as con:
        if alerted:
            con.execute(
                "UPDATE pelops_watchers "
                "SET last_checked_at = ?, last_seen_hash = ?, "
                "    last_seen_response = ?, last_alert_at = ? "
                "WHERE id = ?",
                (now, new_hash, new_response, now, watcher_id),
            )
        else:
            con.execute(
                "UPDATE pelops_watchers "
                "SET last_checked_at = ?, last_seen_hash = ?, "
                "    last_seen_response = ? "
                "WHERE id = ?",
                (now, new_hash, new_response, watcher_id),
            )


def has_changed(watcher: dict, new_response: str) -> bool:
    """True if the normalized hash of new_response differs from last_seen_hash."""
    if not watcher.get("last_seen_hash"):
        # First observation -- count as a change so Pelops introduces the topic.
        return True
    return _hash(new_response) != watcher["last_seen_hash"]
