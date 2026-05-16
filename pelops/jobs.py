"""Cross-process safe scheduled follow-ups via a custom SQLite table.

Why not APScheduler? Multiple Scheduler instances against a shared
persistent store cause race conditions and executor-pool corruption
(observed in this project). The Pelops use case (one-shot or cron-style
follow-ups added by any process, executed by the bot) maps cleanly to a
single SQLite table with claim semantics.

Schema (created lazily on first use):

    pelops_followups
        id          TEXT PRIMARY KEY     -- 'fu_<uuid8>_<label>'
        label       TEXT                  -- short slug
        prompt      TEXT NOT NULL         -- agent prompt to run at fire time
        run_at_utc  TEXT NOT NULL         -- ISO datetime in UTC
        cron        TEXT                  -- if set, re-arm with this cron expr
        created_at  TEXT NOT NULL
        claimed_at  TEXT                  -- NULL = unclaimed, else claimer
        fired_at    TEXT                  -- NULL = not fired yet
        status      TEXT NOT NULL         -- 'pending'|'firing'|'done'|'error'
        last_error  TEXT
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

from pelops.config import Settings

log = logging.getLogger("pelops.jobs")

_FOLLOWUP_PREFIX = "fu_"

# Exponential-ish backoff: 30s, 1m, 5m, 15m, 60m.
# After the last attempt fails, status becomes 'error' (terminal).
RETRY_BACKOFF_SECONDS = [30, 60, 300, 900, 3600]

_PAIR_RE = re.compile(
    r"(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days|week|weeks)",
    re.IGNORECASE,
)
_UNIT_TO_KW = {
    "second": "seconds", "seconds": "seconds",
    "minute": "minutes", "minutes": "minutes",
    "hour": "hours",     "hours": "hours",
    "day": "days",       "days": "days",
    "week": "weeks",     "weeks": "weeks",
}


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
            last_error    TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            response      TEXT,
            watcher_id    TEXT
        )
        """
    )
    # Migrations for older DBs.
    for ddl in (
        "ALTER TABLE pelops_followups ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pelops_followups ADD COLUMN response TEXT",
        "ALTER TABLE pelops_followups ADD COLUMN watcher_id TEXT",
    ):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_pelops_followups_due "
        "ON pelops_followups(status, run_at_utc)"
    )
    return con


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_run_at(when: str) -> tuple[str, str | None]:
    """Parse `when` -> (run_at_utc_iso, cron_or_None).

    Accepted forms:
      - Relative offset (single or compound):
          'in 2 minutes', 'in 3 hours', 'in 1 day',
          'in 15 hours and 29 minutes', 'in 2 days 3 hours'
      - 5-field cron:  '0 9 * * *'  -> first fire = next match
      - ISO datetime:  '2026-05-15T09:00:00'
    """
    when = when.strip()

    # Relative offset, possibly compound. Triggered when the string starts
    # with "in " AND we can extract at least one (N, unit) pair.
    if when.lower().startswith("in "):
        pairs = _PAIR_RE.findall(when)
        if pairs:
            delta_kwargs: dict[str, int] = {}
            for n, unit in pairs:
                kw = _UNIT_TO_KW[unit.lower()]
                delta_kwargs[kw] = delta_kwargs.get(kw, 0) + int(n)
            run_at = datetime.now(timezone.utc) + timedelta(**delta_kwargs)
            return run_at.isoformat(), None

    if len(when.split()) == 5:
        trig = CronTrigger.from_crontab(when)
        next_fire = trig.get_next_fire_time(None, datetime.now(timezone.utc))
        if next_fire is None:
            raise ValueError(f"Cron {when!r} produces no future fire time")
        return next_fire.astimezone(timezone.utc).isoformat(), when

    try:
        run_at = datetime.fromisoformat(when.replace(" ", "T"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        return run_at.astimezone(timezone.utc).isoformat(), None
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse `when`={when!r}. Accepted forms: "
            f"'in 2 minutes', 'in 15 hours and 29 minutes', "
            f"'2026-05-15T09:00:00', '0 9 * * *'."
        ) from exc


def add(
    prompt: str,
    when: str,
    label: str | None = None,
    *,
    watcher_id: str | None = None,
) -> str:
    """Insert a new follow-up. Returns its id.

    If watcher_id is set, the followup is bound to a watcher -- the runner
    will call back into watchers.note_noop/note_alert when it completes.
    """
    label = (label or "followup").strip().replace(" ", "-")[:40]
    job_id = f"{_FOLLOWUP_PREFIX}{uuid.uuid4().hex[:8]}_{label}"
    run_at, cron = _compute_run_at(when)
    with _connect() as con:
        con.execute(
            "INSERT INTO pelops_followups "
            "(id, label, prompt, run_at_utc, cron, created_at, status, watcher_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (job_id, label, prompt, run_at, cron, _now_iso(), watcher_id),
        )
    log.info("added followup %s run_at=%s cron=%s watcher_id=%s",
             job_id, run_at, cron, watcher_id)
    return job_id


def cancel(job_id: str) -> bool:
    with _connect() as con:
        cur = con.execute(
            "UPDATE pelops_followups SET status='cancelled' "
            "WHERE id = ? AND status IN ('pending', 'firing')",
            (job_id,),
        )
        ok = cur.rowcount > 0
    if ok:
        log.info("cancelled followup %s", job_id)
    return ok


def list_pending() -> list[dict]:
    with _connect() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, label, run_at_utc, cron, status "
            "FROM pelops_followups WHERE status = 'pending' "
            "ORDER BY run_at_utc"
        ).fetchall()
    return [dict(r) for r in rows]


def claim_due() -> list[dict]:
    """Atomically claim all pending rows whose run_at_utc has passed.

    Returns the claimed rows. Another caller invoked at the same time will
    NOT see these rows (the UPDATE filter on claimed_at IS NULL is the
    transactional gate).
    """
    claimer = f"pid:{datetime.now(timezone.utc).timestamp()}"
    now = _now_iso()
    with _connect() as con:
        con.row_factory = sqlite3.Row
        # Try to claim. Atomic compare-and-set.
        con.execute(
            "UPDATE pelops_followups "
            "SET claimed_at = ?, status = 'firing' "
            "WHERE status = 'pending' AND claimed_at IS NULL "
            "AND run_at_utc <= ?",
            (claimer, now),
        )
        # Read what we claimed (rows with our claimer marker).
        rows = con.execute(
            "SELECT id, label, prompt, cron, run_at_utc, attempt_count, watcher_id "
            "FROM pelops_followups WHERE claimed_at = ?",
            (claimer,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_done(job_id: str, response: str | None = None) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE pelops_followups "
            "SET status = 'done', fired_at = ?, last_error = NULL, "
            "    claimed_at = NULL, response = ? "
            "WHERE id = ?",
            (_now_iso(), response, job_id),
        )


def recent_completions(hours: int = 24, limit: int = 10) -> list[dict]:
    """Return follow-ups that fired within the last `hours`, newest first.

    Used by the Chainlit UI to show an inbox catch-up when the user opens
    a new session -- so they can see what Pelops pushed to Telegram while
    they were away.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _connect() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, label, prompt, response, fired_at, status, last_error "
            "FROM pelops_followups "
            "WHERE fired_at IS NOT NULL AND fired_at >= ? "
            "AND status IN ('done', 'error') "
            "ORDER BY fired_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_failed(job_id: str, last_error: str) -> dict:
    """Mark a fired job as failed and schedule a retry with backoff.

    If we have already exhausted RETRY_BACKOFF_SECONDS, mark the row as
    permanently 'error' so the poller stops touching it.

    Returns a dict describing what happened: {'retried': bool, 'next_run_at':
    ..., 'attempt': N}.
    """
    with _connect() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT attempt_count FROM pelops_followups WHERE id = ?",
            (job_id,),
        ).fetchone()
        attempt = (row["attempt_count"] if row else 0) + 1
        if attempt > len(RETRY_BACKOFF_SECONDS):
            con.execute(
                "UPDATE pelops_followups "
                "SET status = 'error', fired_at = ?, last_error = ?, "
                "    claimed_at = NULL, attempt_count = ? "
                "WHERE id = ?",
                (_now_iso(), last_error, attempt, job_id),
            )
            log.warning("followup %s gave up after %d attempts", job_id, attempt)
            return {"retried": False, "attempt": attempt, "next_run_at": None}
        backoff_s = RETRY_BACKOFF_SECONDS[attempt - 1]
        next_run = (datetime.now(timezone.utc) + timedelta(seconds=backoff_s)).isoformat()
        con.execute(
            "UPDATE pelops_followups "
            "SET status = 'pending', claimed_at = NULL, "
            "    run_at_utc = ?, last_error = ?, attempt_count = ? "
            "WHERE id = ?",
            (next_run, last_error, attempt, job_id),
        )
    log.info(
        "followup %s scheduled retry #%d in %ds (next_run=%s)",
        job_id, attempt, backoff_s, next_run,
    )
    return {"retried": True, "attempt": attempt, "next_run_at": next_run}


def rearm_cron(job_id: str, cron: str) -> None:
    """For a cron-based followup: compute next run and set status back to pending."""
    trig = CronTrigger.from_crontab(cron)
    nxt = trig.get_next_fire_time(None, datetime.now(timezone.utc))
    if nxt is None:
        mark_done(job_id, last_error="cron exhausted")
        return
    with _connect() as con:
        con.execute(
            "UPDATE pelops_followups "
            "SET status = 'pending', claimed_at = NULL, "
            "    run_at_utc = ?, fired_at = ?, last_error = NULL "
            "WHERE id = ?",
            (nxt.astimezone(timezone.utc).isoformat(), _now_iso(), job_id),
        )
    log.info("re-armed cron followup %s next=%s", job_id, nxt)
