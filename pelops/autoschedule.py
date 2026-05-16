"""Self-scheduling: Pelops can program future actions for itself.

Architecture:
  - One persistent jobstore (SQLite at data/jobs.db) shared across processes.
  - The standalone scheduler (`python -m pelops.scheduler`) owns and executes
    jobs.
  - Clients (chat UI, telegram bot, the agent through its tools) only WRITE
    to the jobstore via `add_followup_job`. The standalone scheduler picks
    them up automatically.

When a job fires, `_run_followup` invokes the agent with the stored prompt and
pushes the result to the owner over Telegram.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from pelops.config import Settings

log = logging.getLogger("pelops.autoschedule")

_FOLLOWUP_PREFIX = "fu_"
PERSISTENT_STORE = "persistent"

# Module-level scheduler singleton. Initialized once per process by the
# entry point (telegram bot or standalone scheduler). Clients that try to
# schedule follow-ups without an initialized scheduler get a clear error.
_scheduler = None


def init_scheduler(blocking: bool = False):
    """Initialize the process-local scheduler singleton. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = make_persistent_scheduler(blocking=blocking)
    return _scheduler


def get_scheduler():
    if _scheduler is None:
        raise RuntimeError(
            "No scheduler initialized in this process. Follow-ups can only "
            "be scheduled from the Telegram bot or the standalone scheduler. "
            "(Chainlit and ad-hoc scripts cannot.)"
        )
    return _scheduler


def _jobstore_url() -> str:
    s = Settings.load()
    db = Path(s.vstash_db).parent / "jobs.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db}"


def make_persistent_scheduler(blocking: bool = False):
    """Build a scheduler with TWO jobstores:

    - `default` (in-memory) -- where the standalone scheduler registers its
      built-in cron jobs (ingest/briefing/consolidate/pulse/health). These
      are re-created at every startup from code, so persistence would just
      cause cross-process restore errors.

    - `persistent` (sqlite at data/jobs.db) -- where dynamic follow-ups
      added through `add_followup_job` live. These survive restarts and are
      visible across all processes (bot, chainlit, scheduler).

    Use blocking=True from the standalone scheduler process; False from
    clients that only schedule and do not run jobs themselves.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    stores = {
        "default": MemoryJobStore(),
        PERSISTENT_STORE: SQLAlchemyJobStore(url=_jobstore_url()),
    }
    cls = BlockingScheduler if blocking else BackgroundScheduler
    return cls(jobstores=stores, timezone="UTC")


def _run_followup(prompt: str, label: str) -> None:
    """Job callback. Runs the agent and pushes the result to Telegram."""
    from pelops.agent import ask
    from pelops.telegram_bot import push_to_owner

    log.info("followup firing: label=%s", label)
    try:
        answer = ask(prompt)
    except Exception as exc:
        log.exception("followup agent invocation failed")
        push_to_owner(f"followup-error ({label})", f"Algo trono: {exc}")
        return
    push_to_owner(f"followup ({label})", answer)


_RELATIVE_RE = re.compile(
    r"^\s*in\s+(?P<n>\d+)\s+"
    r"(?P<unit>second|seconds|minute|minutes|hour|hours|day|days|week|weeks)\s*$",
    re.IGNORECASE,
)

_UNIT_TO_KW = {
    "second": "seconds",
    "seconds": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
}


def _parse_when(when: str) -> tuple[str, dict[str, Any]]:
    """Parse a `when` string into ("date"|"cron", trigger kwargs).

    Accepted forms (in order of preference -- prefer relative for short
    waits to avoid LLM date-arithmetic mistakes):

      - Relative offset: 'in 2 minutes', 'in 3 hours', 'in 1 day', 'in 2 weeks'
      - 5-field cron:    '0 9 * * *', '*/10 * * * *'
      - ISO datetime:    '2026-05-15T09:00:00', '2026-05-15 09:00'
    """
    when = when.strip()

    m = _RELATIVE_RE.match(when)
    if m:
        n = int(m.group("n"))
        kw = _UNIT_TO_KW[m.group("unit").lower()]
        run_at = datetime.now(UTC) + timedelta(**{kw: n})
        return "date", {"trigger": DateTrigger(run_date=run_at)}

    if len(when.split()) == 5:
        return "cron", {"trigger": CronTrigger.from_crontab(when)}

    try:
        run_at = datetime.fromisoformat(when.replace(" ", "T"))
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse `when`={when!r}. Accepted formats: "
            f"relative offset ('in 2 minutes', 'in 3 hours', 'in 1 day'), "
            f"ISO datetime ('2026-05-15T09:00:00'), "
            f"or 5-field cron ('0 9 * * *')."
        ) from exc
    return "date", {"trigger": DateTrigger(run_date=run_at)}


def add_followup_job(prompt: str, when: str, label: str | None = None) -> str:
    """Schedule a future agent invocation on the process-local scheduler.

    The job persists in data/jobs.db and will survive restarts.
    """
    sched = get_scheduler()
    label = (label or "followup").strip().replace(" ", "-")[:40]
    job_id = f"{_FOLLOWUP_PREFIX}{uuid.uuid4().hex[:8]}_{label}"
    _, trigger_kwargs = _parse_when(when)
    sched.add_job(
        _run_followup,
        args=[prompt, label],
        id=job_id,
        jobstore=PERSISTENT_STORE,
        replace_existing=True,
        **trigger_kwargs,
    )
    log.info("added followup job %s for %s", job_id, when)
    return job_id


def cancel_followup_job(job_id: str) -> bool:
    sched = get_scheduler()
    try:
        sched.remove_job(job_id, jobstore=PERSISTENT_STORE)
        log.info("removed followup job %s", job_id)
        return True
    except Exception as exc:
        log.warning("remove_job(%s) failed: %s", job_id, exc)
        return False


def list_followup_jobs() -> list[dict[str, str]]:
    sched = get_scheduler()
    out = []
    for j in sched.get_jobs(jobstore=PERSISTENT_STORE):
        if not j.id.startswith(_FOLLOWUP_PREFIX):
            continue
        next_run = getattr(j, "next_run_time", None)
        out.append(
            {
                "id": j.id,
                "next_run": str(next_run) if next_run else "(pending)",
                "trigger": str(j.trigger),
            }
        )
    return out
