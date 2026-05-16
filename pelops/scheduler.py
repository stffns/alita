"""APScheduler-driven autonomous loop for Pelops.

Three jobs:
  ingest        scan configured RSS feeds, store fresh items
  briefing      every morning, summarize new stuff since yesterday
  consolidate   nightly distillation of episodic notes into semantic ones
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.triggers.cron import CronTrigger

from pelops import jobs, watchers
from pelops.agent import ask
from pelops.autoschedule import init_scheduler
from pelops.config import Settings
from pelops.memory import get_memory
from pelops.telegram_bot import push_to_owner
from pelops.tools import fetch_rss, record_agent_action, research, vstash_remember

log = logging.getLogger("pelops.scheduler")


def _cron(expr: str) -> CronTrigger:
    return CronTrigger.from_crontab(expr)


def job_ingest() -> None:
    s = Settings.load()
    if not s.rss_feeds:
        log.info("ingest: no feeds configured, skipping")
        return
    log.info("ingest: scanning %d feed(s)", len(s.rss_feeds))
    for feed in s.rss_feeds:
        summary = fetch_rss.invoke({"feed_url": feed, "limit": 10})
        title = f"rss_{datetime.now().strftime('%Y%m%d_%H%M')}_{feed[:40]}"
        vstash_remember.invoke(
            {
                "content": summary,
                "title": title,
                "layer": "rss",
                "tags": "rss,ingest",
            }
        )
    log.info("ingest: done")


def job_briefing() -> None:
    s = Settings.load()
    topics = ", ".join(s.topics) if s.topics else "anything notable"
    prompt = (
        f"Produce a morning briefing for {s.owner}.\n\n"
        f"STEPS:\n"
        f"0. BEFORE composing, call vstash_recall(query='briefing', "
        f"layer='agent-action', top_k=3) to see what you already pushed in "
        f"the last few briefings. DO NOT REPEAT items you already covered. "
        f"If today has nothing new beyond yesterday, surface that explicitly "
        f"('nothing notable beyond yesterday') instead of recycling.\n"
        f"1. Call vstash_recall once per topic in: {topics}. Use top_k=5.\n"
        f"2. Cluster the results by theme. Drop noise.\n"
        f"3. Compose the brief in this EXACT plain-text shape (no markdown, "
        f"no asterisks, no brackets -- this will be sent to Telegram which "
        f"renders as raw text):\n\n"
        f"   Briefing YYYY-MM-DD\n"
        f"   ===================\n\n"
        f"   * <Theme 1>\n"
        f"     <one-line summary>\n"
        f"     Why it matters: <one line>\n\n"
        f"   * <Theme 2>\n"
        f"     <one-line summary>\n"
        f"     Why it matters: <one line>\n\n"
        f"   (3-5 themes total)\n\n"
        f"4. Call vstash_remember with content=<the brief above>, "
        f"title='briefing_YYYY-MM-DD', tags='briefing'.\n"
        f"5. RETURN THE BRIEF VERBATIM as your final message. Do NOT replace it "
        f"with 'saved successfully' or any confirmation -- the brief itself is "
        f"the output. The save is a side effect.\n\n"
        f"FORMATTING RULES (strict):\n"
        f"- No `**bold**`, no `_italic_`, no `[links](url)`. Plain ASCII only.\n"
        f"- Use blank lines between themes for breathing room.\n"
        f"- Keep each line under 80 characters."
    )
    answer = ask(prompt)
    log.info("briefing:\n%s", answer)
    _push_to_owner("briefing", answer)


def _push_to_owner(label: str, body: str) -> None:
    """Push a scheduler job's output to Telegram (no-op if Telegram is not configured).

    Also records the push as an 'agent-action' memory so future Pelops
    knows what has already been said and can avoid repetition.
    """
    try:
        from pelops.telegram_bot import push_to_owner
    except ImportError:
        return
    try:
        push_to_owner(label, body)
        record_agent_action(label, body)
    except Exception as exc:
        log.warning("telegram push failed (%s): %s", label, exc)


def job_consolidate() -> None:
    prompt = (
        "Nightly consolidation. Use vstash_recall to look at recent ingest notes "
        "(tag 'rss' or 'ingest'). For any cluster of related items, write one "
        "consolidated semantic note that captures the trend, and save it via "
        "vstash_remember with tags 'consolidated'. Be ruthless about signal vs noise.\n\n"
        "RETURN a short plain-text summary of what you consolidated -- format:\n\n"
        "  Consolidation YYYY-MM-DD\n"
        "  ========================\n\n"
        "  Created N consolidated notes:\n"
        "  - <title 1>\n"
        "  - <title 2>\n\n"
        "  Discarded: <one-line about what you dropped as noise>\n\n"
        "No markdown. Plain ASCII only. This goes to Telegram."
    )
    answer = ask(prompt)
    log.info("consolidate:\n%s", answer)
    _push_to_owner("consolidate", answer)


def job_health() -> None:
    """Cheap heartbeat -- proves the scheduler is alive without LLM cost."""
    stats = get_memory().stats()
    log.info("health: vstash docs=%s", getattr(stats, "documents", "?"))


def job_poll_followups() -> None:
    """Poll the pelops_followups table for due rows and execute them.

    Cross-process safe: any process can INSERT into the table; this poller
    in the bot/scheduler claims and executes. Claim is atomic via SQL.
    """
    claimed = jobs.claim_due()
    for row in claimed:
        job_id = row["id"]
        prompt = row["prompt"]
        label = row["label"]
        cron = row["cron"]
        attempt = row.get("attempt_count", 0) + 1
        log.info("followup firing: id=%s label=%s attempt=%d", job_id, label, attempt)
        try:
            # restricted=True drops the scheduling tool so a follow-up cannot
            # recursively spawn more follow-ups (Hermes-style runaway guard).
            answer = ask(prompt, restricted=True)
            watcher_id = row.get("watcher_id")
            is_noop = answer.strip().upper() == "NOOP"
            if is_noop:
                log.info("followup %s returned NOOP -- skipping push", job_id)
            else:
                push_to_owner(f"followup ({label})", answer)
                record_agent_action(f"followup_{label}", answer)
            # If this follow-up was bound to a watcher, update its cadence.
            if watcher_id:
                if is_noop:
                    delta = watchers.note_noop(watcher_id)
                else:
                    delta = watchers.note_alert(watcher_id)
                if delta.get("changed"):
                    direction = delta["direction"]
                    old_h = watchers._fmt_interval(delta["old_interval"])
                    new_h = watchers._fmt_interval(delta["new_interval"])
                    verb = (
                        "este tema cambia menos de lo que pensaba, paso a chequear"
                        if direction == "slower"
                        else "este tema se desperto, vuelvo a chequear"
                    )
                    adjust_msg = f"{verb} {new_h} (antes {old_h})."
                    push_to_owner(f"watcher-adjust ({label})", adjust_msg)
                    record_agent_action(f"watcher_adjust_{label}", adjust_msg)
        except Exception as exc:
            log.exception("followup execution failed: %s", job_id)
            result = jobs.mark_failed(job_id, last_error=str(exc))
            if not result["retried"]:
                push_to_owner(
                    f"followup-failed ({label})",
                    f"Job {job_id} gave up after {result['attempt']} attempts.\nLast error: {exc}",
                )
            continue
        if cron:
            jobs.rearm_cron(job_id, cron)
        else:
            jobs.mark_done(job_id, response=answer)


# Watcher alerts: if the agent decided the change was noise it returns NOOP.
# Wrap the push call in the followup runner so we can short-circuit.


def job_session_snapshot() -> None:
    """Compose a rolling 'where we left off' note from recent chat turns.

    Cheap: only fires the agent if there's been any episodic turn in the
    last 30 min. Otherwise no-op. Saves to layer='session-state' so future
    sessions can pick up the thread.
    """
    from datetime import datetime, timedelta

    from pelops.memory import get_memory

    mem = get_memory()
    datetime.now(UTC) - timedelta(minutes=30)
    recent = mem.search(
        "chat turn",
        top_k=10,
        layer="episodic",
    )
    if not recent:
        log.info("session-snapshot: no recent episodic turns, skipping")
        return
    # Build a compact transcript for the agent to summarize.
    lines = []
    for r in recent[:8]:
        text = (getattr(r, "text", "") or "")[:500]
        lines.append(text)
    transcript = "\n---\n".join(lines)

    prompt = (
        "Below are the last several chat turns between Jay and you. Compose "
        "a 4-6 line plain-text snapshot of WHERE WE ARE RIGHT NOW: what we "
        "were doing, what is open, what Jay seemed to care about most. "
        "Write it in second person ('we were exploring X', 'you raised Y'). "
        "After composing, save it to vstash with vstash_remember(layer='session-state', "
        "title='session_state_latest', tags='session-state'). RETURN the "
        "snapshot text verbatim.\n\n"
        f"Recent transcript:\n{transcript}"
    )
    answer = ask(prompt, restricted=True)
    log.info("session-snapshot composed:\n%s", answer[:300])


def job_pulse() -> None:
    """Proactive morning ping. Pelops decides what to mention based on memory."""
    s = Settings.load()
    topics = ", ".join(s.topics) if s.topics else "your usual topics"
    prompt = (
        f"You are sending a proactive morning ping to {s.owner}. This is not a "
        f"response to a question -- you are starting the conversation.\n\n"
        f"STEPS:\n"
        f"0. BEFORE composing, call vstash_recall(query='pulse', "
        f"layer='agent-action', top_k=3) to see your last few pulses. DO NOT "
        f"repeat the topic you mentioned yesterday. If yesterday was about "
        f"deepagents, today should be about something else -- or be honest: "
        f"'morning, still chewing on yesterday's thread, no new angle yet'.\n"
        f"1. Use vstash_recall to find what was discussed or researched recently "
        f"(queries: '{s.owner}', 'recent research', '{topics}').\n"
        f"2. Use vstash_recall to scan the latest consolidated notes (layer "
        f"'consolidated').\n"
        f"3. Pick ONE thing worth mentioning today -- something that would change "
        f"what {s.owner} does today or that follows up on something they cared "
        f"about. Quality over quantity.\n"
        f"4. If there is a sensible follow-up that justifies more autonomy "
        f"(e.g. 'check back in 2 days on this paper'), call schedule_followup "
        f"with a clear future-prompt and an ISO datetime in UTC.\n"
        f"5. RETURN your morning message in plain text (no markdown, no asterisks, "
        f"no brackets). Tone: warm, brief, dog-like Pelops. 4-6 lines max.\n"
        f"If you found nothing worth surfacing today, just send a one-line "
        f"'morning, nothing notable yet -- still listening' and stop."
    )
    answer = ask(prompt)
    log.info("pulse:\n%s", answer)
    _push_to_owner("pulse", answer)


def register_cron_jobs(sched) -> None:
    """Register the built-in cron jobs against a scheduler.

    Uses the in-memory `default` jobstore so these are recreated at every
    startup and never serialized to the persistent store.
    """
    s = Settings.load()
    sched.add_job(job_ingest, _cron(s.ingest_cron), id="ingest", replace_existing=True)
    sched.add_job(job_briefing, _cron(s.briefing_cron), id="briefing", replace_existing=True)
    sched.add_job(
        job_consolidate, _cron(s.consolidate_cron), id="consolidate", replace_existing=True
    )
    sched.add_job(job_pulse, _cron(s.pulse_cron), id="pulse", replace_existing=True)
    sched.add_job(job_health, _cron("*/15 * * * *"), id="health", replace_existing=True)
    # Session-state snapshot: every 30 min, if there was recent chat activity,
    # roll the episodic turns into a single state note Pelops reads at session
    # start. Makes the agent feel continuous across sessions.
    sched.add_job(
        job_session_snapshot,
        _cron("*/30 * * * *"),
        id="session_snapshot",
        replace_existing=True,
    )


def register_followup_poller(sched, interval_seconds: int = 30) -> None:
    """Register the followup-table poller. This is what makes
    cross-process-scheduled follow-ups actually fire."""
    from apscheduler.triggers.interval import IntervalTrigger

    sched.add_job(
        job_poll_followups,
        IntervalTrigger(seconds=interval_seconds),
        id="followup_poll",
        replace_existing=True,
    )


def register_watcher_poller(sched, interval_seconds: int = 60) -> None:
    """Register the watcher poller. Pelops re-checks each watcher's query on
    its individual interval; if the result changed, fires an autonomous
    alert.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    sched.add_job(
        job_poll_watchers,
        IntervalTrigger(seconds=interval_seconds),
        id="watcher_poll",
        replace_existing=True,
    )


def job_poll_watchers() -> None:
    """For each due watcher: re-run the query and decide if it warrants an
    autonomous alert.

    First observation -> ALWAYS introduce the topic to the owner (one alert
    composed from the raw research).
    Subsequent observations -> alert only if the diff is substantive; the
    agent decides via the NOOP convention.
    """
    due = watchers.claim_due()
    if not due:
        return
    for w in due:
        wid = w["id"]
        try:
            current = research.invoke({"query": w["query"], "deep": False})
        except Exception as exc:
            log.warning("watcher %s research failed: %s", wid, exc)
            continue

        is_first = not w.get("last_seen_hash")

        if not is_first and not watchers.has_changed(w, current):
            watchers.update_state(wid, current, alerted=False)
            log.info("watcher %s: no change", wid)
            continue

        if is_first:
            prompt = (
                f"Pelops watcher '{w['label']}' is reporting its FIRST "
                f"observation. This is the baseline -- introduce the topic to "
                f"Jay so future alerts make sense as deltas.\n\n"
                f"QUERY: {w['query']}\n\n"
                f"CURRENT observation:\n{current}\n\n"
                f"Compose a 2-3 line introductory alert in plain text (no "
                f"markdown). Lead with what you found, then say you will "
                f"keep watching. Do NOT return NOOP on a first run -- the "
                f"point of this alert is to confirm Pelops is now tracking "
                f"this topic."
            )
        else:
            previous = w["last_seen_response"]
            prompt = (
                f"Pelops watcher '{w['label']}' detected a possible change "
                f"between two observations.\n\n"
                f"QUERY: {w['query']}\n\n"
                f"PREVIOUS observation:\n{previous}\n\n"
                f"CURRENT observation:\n{current}\n\n"
                f"BEFORE deciding, call vstash_recall(query='{w['label']}', "
                f"layer='agent-action', top_k=3) so you can see what alerts "
                f"you already pushed for this watcher. If the 'new' fact is "
                f"something you already mentioned, treat it as NOOP.\n\n"
                f"Decide if the change is meaningful for Jay. Meaningful = a "
                f"new release, a new finding, a substantive update, or a "
                f"notable retraction. NOT meaningful = wording differences, "
                f"reordering, timestamp drift, the same facts said "
                f"differently, OR facts you have already alerted on.\n\n"
                f"If MEANINGFUL: compose a 2-3 line alert in plain text. "
                f"Lead with the punchline.\n"
                f"If NOT MEANINGFUL: your ENTIRE response must be the single "
                f"token NOOP -- nothing else."
            )

        jobs.add(
            prompt=prompt,
            when="in 5 seconds",
            label=f"watch-{w['label']}",
            watcher_id=wid,
        )
        watchers.update_state(wid, current, alerted=True)
        log.info(
            "watcher %s: %s -> alert scheduled",
            wid,
            "first observation" if is_first else "change detected",
        )


def build_scheduler():
    sched = init_scheduler(blocking=True)
    register_cron_jobs(sched)
    return sched


def main() -> None:
    from pelops.logging_config import configure as configure_logging

    configure_logging(level=logging.INFO)
    sched = build_scheduler()
    log.info("Pelops scheduler starting. Jobs:")
    for j in sched.get_jobs():
        log.info("  %s -> %s", j.id, j.trigger)
    sched.start()


if __name__ == "__main__":
    main()
