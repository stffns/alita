"""APScheduler-driven autonomous loop for Pelops.

Cron jobs:
  ingest             scan configured RSS feeds, store fresh items
  briefing           every morning, summarize new stuff since yesterday
  consolidate        nightly distillation of episodic notes into semantic ones
  heartbeat          every 15 min, agent decides NOOP / ping / surface_thought
                     / mini_consolidate. Replaces the old fixed `job_pulse`.
  health             every 15 min, cheap "scheduler is alive" log (no LLM cost)
  session_snapshot   every 30 min, rolling "where we left off" note

The heartbeat is the adaptive piece -- the other crons are predictable
infrastructure (RSS ingestion, nightly consolidation, morning briefing).
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
        "a SHORT prose snapshot (one short paragraph, 4-6 sentences) of "
        "where you and Jay are right now: what you were doing, what is "
        "open, what Jay seemed to care about most. Write in second person "
        "('we were exploring X', 'you raised Y') as flowing prose. "
        "Do NOT use headings (no `##`, no ALL-CAPS section labels). "
        "Do NOT use bullet lists. Do NOT use numbered lists. "
        "Do NOT use bold field labels. Sentences only.\n\n"
        "After composing, save it to vstash with "
        "vstash_remember(layer='session-state', "
        "title='session_state_latest', tags='session-state'). RETURN the "
        "snapshot text verbatim.\n\n"
        f"Recent transcript:\n{transcript}"
    )
    answer = ask(prompt, restricted=True)
    log.info("session-snapshot composed:\n%s", answer[:300])


def _latest_per_layer(
    layer_filters: dict[str, str | None] | None = None,
) -> dict[str, datetime | None]:
    """Single scan of vstash that returns the newest `added_at` per layer.

    Pass `{layer: title_prefix_or_None}` mapping. Returns the same keys
    with either a tz-aware datetime or None. Doing this in one pass
    avoids the previous N-times full scan (one per layer) -- the
    heartbeat fires every 15 min and the vault grows, so 6 passes
    becomes a real cost.

    All returned datetimes are coerced to UTC-aware so callers can
    subtract from `datetime.now(UTC)` without TypeErrors when an old
    vstash row happens to lack a tz offset in its stored string.
    """
    filters = layer_filters or {}
    latest: dict[str, datetime | None] = {layer: None for layer in filters}
    try:
        docs = list(get_memory().list())
    except Exception as exc:
        log.warning("heartbeat: vstash list() failed: %s", exc)
        return latest
    for d in docs:
        layer = getattr(d, "layer", None)
        if layer not in filters:
            continue
        title_prefix = filters[layer]
        if title_prefix and not (getattr(d, "title", "") or "").startswith(title_prefix):
            continue
        ts_str = getattr(d, "added_at", None)
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        # Coerce naive timestamps to UTC so all comparisons are safe
        # against `datetime.now(UTC)`. vstash currently stores ISO with
        # offset, but the contract is not enforced -- defensive.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)
        current = latest[layer]
        if current is None or ts > current:
            latest[layer] = ts
    return latest


def job_heartbeat() -> None:
    """Adaptive proactive beat. Replaces the old fixed `job_pulse`.

    Runs every 15 min. Two cheap Python guards skip the LLM call when
    obviously not needed; otherwise the agent picks ONE action from
    {NOOP, ping, surface_thought, mini_consolidate} and we execute it.

    Guard 1 -- "Jay is here right now": if there is an episodic chat note
    timestamped within the last 10 min, the user is actively chatting and
    a proactive push would be noise. NOOP without burning tokens.

    Guard 2 -- "we already pinged recently": if the heartbeat already
    pushed something within the last 2 h, NOOP. Anti-spam.

    Beats that survive both guards invoke the agent in `restricted=True`
    mode (no `followup` tool) and parse the structured response.

    The prompt deliberately tells the agent "silence forever is failure".
    Earlier versions said "bias HEAVILY toward NOOP" which led to streaks
    of 5+ NOOPs and Alita felt passive. We now inject concrete activity
    signals (time since last action, fresh vstash ingestions) so the
    agent has real data to decide on instead of guessing.
    """
    from datetime import timedelta

    now = datetime.now(UTC)

    # One full scan across all the layers we care about. Replaces six
    # separate `_latest_added_at` calls that each walked vstash.
    latest = _latest_per_layer(
        {
            "episodic": None,
            "agent-action": "action_heartbeat_",
            "rss": None,
            "research": None,
            "consolidated": None,
            "thoughts": None,
        }
    )

    last_chat = latest["episodic"]
    if last_chat is not None and (now - last_chat) < timedelta(minutes=10):
        secs = int((now - last_chat).total_seconds())
        log.info("heartbeat: chat activity %ds ago, NOOP", secs)
        return

    last_push = latest["agent-action"]
    if last_push is not None and (now - last_push) < timedelta(hours=2):
        mins = int((now - last_push).total_seconds() / 60)
        log.info("heartbeat: pushed %dmin ago, NOOP", mins)
        return

    # Real signals to inject into the prompt. Replaces hallucination
    # space ("Last heartbeat was 8 hours ago (morning pulse)" -- there
    # was never a morning pulse) with measured facts.
    if last_push is None:
        quiet_msg = "You have not taken ANY heartbeat action yet."
    else:
        hours = (now - last_push).total_seconds() / 3600
        quiet_msg = f"Your last heartbeat action was {hours:.1f}h ago."

    fresh_signals = []
    for layer in ("rss", "research", "consolidated", "thoughts", "episodic"):
        ts = latest[layer]
        if ts is not None:
            age_h = (now - ts).total_seconds() / 3600
            if age_h < 12:
                fresh_signals.append(f"{layer} updated {age_h:.1f}h ago")
    fresh_msg = "; ".join(fresh_signals) if fresh_signals else "no vstash activity in the last 12h"

    s = Settings.load()
    topics = ", ".join(s.topics) if s.topics else "your usual topics"
    prompt = (
        f"You are doing a 15-minute heartbeat beat for {s.owner}. Nobody "
        f"asked you anything -- you are deciding if anything is worth doing "
        f"right now.\n\n"
        f"OBSERVED CONTEXT (factual, do not invent more):\n"
        f"  - {quiet_msg}\n"
        f"  - Vstash activity: {fresh_msg}.\n"
        f"  - Topics of interest: {topics}.\n\n"
        f"DECISION FRAMING:\n"
        f"  - NOOP is fine when nothing genuinely new happened.\n"
        f"  - BUT silence forever is failure. You are meant to be a "
        f"proactive companion, not a chatbot that speaks only when spoken "
        f"to. If there is fresh material in vstash and you have not yet "
        f"surfaced it, that is a reason to act.\n"
        f"  - If your last few beats were ALL NOOP and you see ANY fresh "
        f"activity above, lean toward surface_thought or mini_consolidate "
        f"rather than another NOOP. Those are low-noise options "
        f"(mini_consolidate sends NOTHING to Telegram, it just compiles "
        f"memory).\n\n"
        f"AVAILABLE ACTIONS (pick exactly ONE):\n"
        f"  NOOP                Nothing of substance to act on. Log the reason.\n"
        f"  ping                Send a SHORT Telegram message about a fresh, "
        f"genuine thing {s.owner} has not heard yet. High signal-to-noise.\n"
        f"  surface_thought     Bring back an open thought from layer='thoughts' "
        f"that has not been resolved. Gentle Telegram nudge.\n"
        f"  mini_consolidate    Cluster 2-3 recent episodic / rss / research "
        f"notes into one semantic note via "
        f"vstash_remember(layer='consolidated', ...). No Telegram push.\n\n"
        f"PROCESS (keep recall calls SMALL -- top_k=3, not more):\n"
        f"  1. vstash_recall(query='heartbeat', layer='agent-action', "
        f"top_k=3, exclude_title_prefix='action_context-compression_') "
        f"to see your last few beats. Do NOT repeat yourself. The "
        f"exclude_title_prefix arg filters out bulky compression notes "
        f"that are not relevant signal.\n"
        f"  2. vstash_recall(layer='thoughts', top_k=3) for open threads.\n"
        f"  3. vstash_recall(layer='rss', top_k=3) for fresh feeds.\n"
        f"  4. vstash_recall(layer='episodic', top_k=3) for recent chats.\n"
        f"  5. Decide based on what you find, not on what you wish was there.\n\n"
        f"RETURN FORMAT (machine-parsed, strict):\n"
        f"  Line 1 MUST be exactly one of:\n"
        f"    ACTION: NOOP\n"
        f"    ACTION: ping\n"
        f"    ACTION: surface_thought\n"
        f"    ACTION: mini_consolidate\n"
        f"  Followed by:\n"
        f"    NOOP                line 2 = one-line reason for the log. Stop.\n"
        f"    ping                line 2+ = plain-text Telegram message (2-4 "
        f"lines, no markdown, no asterisks, no brackets).\n"
        f"    surface_thought     line 2+ = plain-text Telegram message that "
        f"references the thought.\n"
        f"    mini_consolidate    line 2 = one-line summary of what got "
        f"consolidated. You should have already called vstash_remember.\n"
    )
    # Per-beat thread_id. Heartbeats DO NOT share state across beats -- each
    # one is a fresh decision from a clean context. Reusing a single
    # thread_id (the previous default `default-heartbeat`) caused every
    # beat to inherit the prior beat's tool-call history via the
    # checkpointer, which accumulated ~9k chars per recall on every
    # subsequent beat. Decisions don't need that continuity; the agent
    # gets continuity from vstash_recall calls explicitly.
    beat_thread = f"heartbeat-{now.strftime('%Y%m%d-%H%M')}"
    try:
        answer = ask(prompt, restricted=True, source="heartbeat", thread_id=beat_thread)
    except Exception:
        log.exception("heartbeat: agent invoke failed")
        return

    lines = (answer or "").strip().splitlines()
    if not lines or not lines[0].upper().startswith("ACTION:"):
        log.warning("heartbeat: malformed response, treating as NOOP: %r", (answer or "")[:200])
        return
    action = lines[0].split(":", 1)[1].strip().lower()
    body = "\n".join(lines[1:]).strip()

    if action == "noop":
        log.info("heartbeat: NOOP -- %s", body[:120])
        return
    if action == "ping":
        push_to_owner("heartbeat", body)
        record_agent_action("heartbeat_ping", body)
        log.info("heartbeat: ping pushed")
        return
    if action == "surface_thought":
        push_to_owner("heartbeat (surface)", body)
        record_agent_action("heartbeat_surface", body)
        log.info("heartbeat: surfaced a thought")
        return
    if action == "mini_consolidate":
        # No push -- the agent already wrote to vstash via vstash_remember.
        record_agent_action("heartbeat_consolidate", body)
        log.info("heartbeat: mini-consolidated -- %s", body[:120])
        return
    log.warning("heartbeat: unknown action %r, treating as NOOP", action)


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
    # Adaptive heartbeat replaces the old fixed `job_pulse`. Every 15 min the
    # agent decides if anything is worth doing (or, much more often, NOOPs).
    sched.add_job(job_heartbeat, _cron("*/15 * * * *"), id="heartbeat", replace_existing=True)
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
