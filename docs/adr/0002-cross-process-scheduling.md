# 2. Cross-process scheduling via custom SQLite table, not APScheduler's persistent jobstore

- Status: Accepted
- Date: 2026-05-14
- Deciders: Jay

## Context

Pelops needs follow-ups that any process (Telegram bot, Chainlit UI,
ad-hoc CLI) can schedule but only ONE process executes. The natural fit
seemed to be APScheduler with a `SQLAlchemyJobStore` -- it persists
jobs, multiple schedulers can in theory share the store.

In practice we hit two cliffs:

1. APScheduler pickles the job function reference using `module:func`
   notation. When the standalone scheduler ran with `__name__ ==
   "__main__"`, references stored as `__main__:job_briefing` and the
   bot process (where `__main__` is `pelops.telegram_bot`) could not
   resolve them. APScheduler then *deleted* the jobs as "unrestorable".

2. Multiple Scheduler instances against the same persistent store cause
   executor-pool corruption ("cannot schedule new futures after
   shutdown") when one shuts down. The docs explicitly warn against it.

## Decision

We replaced APScheduler's persistent jobstore with a hand-rolled SQLite
table (`pelops_followups`) and a single in-process poller that lives in
the Telegram bot. Cron jobs (briefing, pulse, ingest, consolidate) stay
on APScheduler with its **in-memory** jobstore -- they are re-registered
at startup and never serialized.

Schema is managed by the project's own forward-only migrations module.

## Consequences

- Trivial cross-process semantics: any process can `INSERT` into the
  table; only the bot's poller `UPDATE`s with claim semantics
  (`UPDATE ... WHERE claimed_at IS NULL`).
- We give up: cron-style recurring follow-ups (only one-shot is
  supported -- a "cron" field exists but rearming logic could be tighter).
- We give up: APScheduler's date/interval primitives (we recreated a
  small subset).
- The bot is the single point of follow-up execution. If the bot is down
  the queue piles up; the followup fires on next bot start (within
  misfire grace).
- Future: if we ever want multi-host execution we will need a real job
  queue (Redis + RQ, Temporal). For one laptop with launchd, this is
  overkill we are happy to defer.

## Alternatives considered

- **APScheduler + persistent SQLAlchemy store**: described above, broke.
- **Celery / RQ / Dramatiq**: heavyweight; introduces a Redis or broker
  dependency for what is essentially `SELECT WHERE run_at_utc <= now`.
- **Temporal**: right answer for production multi-process; wrong-sized
  for a personal companion.
