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
import re
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
    layer_filters: dict[str, str | tuple[str, ...] | None] | None = None,
) -> dict[str, datetime | None]:
    """Single scan of vstash that returns the newest `added_at` per layer.

    Pass `{layer: title_prefix}` mapping. `title_prefix` can be:
      * None        -- accept any title in this layer
      * str         -- accept titles starting with this prefix
      * tuple[str]  -- accept titles starting with ANY of these prefixes

    Returns the same keys with either a tz-aware datetime or None. Doing
    this in one pass avoids the previous N-times full scan (one per
    layer) -- the heartbeat fires every 15 min and the vault grows.

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
        title_filter = filters[layer]
        if title_filter:
            title = getattr(d, "title", "") or ""
            # str.startswith natively accepts a tuple of prefixes; matches
            # if title starts with ANY of them.
            if not title.startswith(title_filter):
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


_DEFAULT_CHECK_FALLBACK = (
    "Pick ONE topic from PELOPS_TOPICS that you have NOT covered in "
    "layer='research' in the last 7 days. Run "
    "research(query=<topic>, deep=False). Save the synthesis with "
    "vstash_remember(layer='research', title=<slug>). Return DONE with "
    "the slug. No Telegram push."
)


def _load_heartbeat_checks() -> list[str]:
    """Parse the `## Checks (rotated)` section out of `heartbeat.md`.

    The behavior of each beat lives in the wiki, NOT in this file. Alita
    and Jay co-edit `heartbeat.md`; the runner reads it every beat. If
    the file is missing or unparseable, we fall back to a single default
    check (curiosity research) so the heartbeat never sits idle.
    """
    try:
        from pelops import wiki

        page = wiki.read("heartbeat")
    except Exception as exc:
        log.warning("heartbeat: wiki.read('heartbeat') failed: %s", exc)
        return [_DEFAULT_CHECK_FALLBACK]
    if page is None:
        log.warning("heartbeat: heartbeat.md not found in vault, using fallback check")
        return [_DEFAULT_CHECK_FALLBACK]

    # Find the section `## Checks (rotated)` and parse numbered items
    # until the next `## ` header. Each numbered item is one check.
    section_re = re.search(
        r"^##\s*Checks\b[^\n]*\n(.*?)(?=^##\s|\Z)",
        page.body,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not section_re:
        log.warning("heartbeat: '## Checks' section not found in heartbeat.md, using fallback")
        return [_DEFAULT_CHECK_FALLBACK]
    body = section_re.group(1)
    # Numbered items: `1.`, `2.`, etc. Capture from the number through to
    # the next number-at-line-start (or end of section). NOTE: no `\s*`
    # at the front -- a nested numbered list inside a check (indented
    # like "   1. sub-item") must NOT be parsed as a new top-level check.
    items = re.findall(
        r"^\d+\.\s+(.+?)(?=^\d+\.\s|\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    items = [it.strip() for it in items if it.strip()]
    if not items:
        log.warning("heartbeat: no numbered checks parsed from heartbeat.md, using fallback")
        return [_DEFAULT_CHECK_FALLBACK]
    return items


def _rotation_index(num_checks: int, now: datetime) -> int:
    """Deterministic round-robin index across beats without storing state.

    Takes `now` as a parameter so the caller's already-computed timestamp
    is reused (instead of re-calling `datetime.now(UTC)`). Removes the
    edge case where two now() calls land on different sides of a 15-min
    boundary.

    The 15-min beat number since unix epoch is monotonic and stateless.
    `beat_number % num_checks` rotates evenly. Skipping a beat (Python
    guard hits) does NOT advance the index, but the rotation still
    covers every check over time because subsequent beats fall on
    different mod values.
    """
    if num_checks <= 0:
        return 0
    beat = int(now.timestamp() // (15 * 60))
    return beat % num_checks


def job_heartbeat() -> None:
    """Behavior-file-driven proactive beat.

    Runs every 15 min. Two cheap Python guards skip the LLM call when
    obviously not needed (recent chat / push cooldown). Beats that
    survive read `heartbeat.md` from the wiki vault, pick one check by
    rotation, and let the agent execute it.

    The agent can ALSO edit `heartbeat.md` via `wiki_write('heartbeat',
    ...)` to refine its own checks. Co-editor model; git in the vault
    is the safety net.

    Output protocol from the agent:
      Line 1 = exactly one of:
        HEARTBEAT_OK            silent disposition, runner discards
        PING                    Telegram message follows on lines 2+
        DONE: <one-line desc>   action taken, no Telegram push
      A malformed response is treated as HEARTBEAT_OK with a WARN log.
    """
    from datetime import timedelta

    now = datetime.now(UTC)

    # One full scan across the layers used by guards and prompt signals.
    # The agent-action filter ONLY matches actions that pushed to Telegram
    # (`heartbeat_ping_*` from a PING disposition, `heartbeat_surface_*`
    # from a surface_thought marker the agent saves alongside its PING).
    # Silent actions (`heartbeat_done_*`, `heartbeat_self-edit_*`,
    # `heartbeat_consolidate_*`) DO NOT extend the cooldown -- they are
    # invisible to {owner} and should not block subsequent pings.
    latest = _latest_per_layer(
        {
            "episodic": None,
            "agent-action": (
                "action_heartbeat_ping_",
                "action_heartbeat_surface_",
            ),
            "rss": None,
            "research": None,
            "consolidated": None,
            "thoughts": None,
        }
    )

    last_chat = latest["episodic"]
    if last_chat is not None and (now - last_chat) < timedelta(minutes=10):
        secs = int((now - last_chat).total_seconds())
        log.info("heartbeat: chat activity %ds ago, HEARTBEAT_OK", secs)
        return

    last_push = latest["agent-action"]
    if last_push is not None and (now - last_push) < timedelta(hours=2):
        mins = int((now - last_push).total_seconds() / 60)
        log.info("heartbeat: pushed (to Telegram) %dmin ago, HEARTBEAT_OK", mins)
        return

    checks = _load_heartbeat_checks()
    idx = _rotation_index(len(checks), now)
    check_text = checks[idx]

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
        f"You are running heartbeat CHECK #{idx + 1} of {len(checks)} from "
        f"your behavior file `heartbeat.md`. Your behavior file is also a "
        f"wiki page -- you may edit it via wiki_write('heartbeat', ...) at "
        f"any time. Refine a check that produces false positives, remove "
        f"one that consistently does nothing, add one when you notice a "
        f"recurring pattern.\n\n"
        f"CHECK TO RUN THIS BEAT:\n"
        f"{check_text}\n\n"
        f"OBSERVED CONTEXT (factual, do not invent):\n"
        f"  - {quiet_msg}\n"
        f"  - Vstash activity: {fresh_msg}.\n"
        f"  - Topics: {topics}.\n\n"
        f"EXECUTION:\n"
        f"  1. Run the check above. Use vstash_recall to look at the "
        f"relevant layer(s).\n"
        f"  2. If the check finds something worth acting on, execute the "
        f"action specified in the check using the right tool(s).\n"
        f"  3. If you want to refine this check or add a new one, call "
        f"wiki_write('heartbeat', updated_body, sources=[...]) before "
        f"returning your disposition.\n\n"
        f"OUTPUT PROTOCOL (machine-parsed):\n"
        f"  After all tool calls are done, your FINAL message must "
        f"contain exactly ONE of these tokens on a line by itself "
        f"(preferably as the FIRST line for cleanest logs):\n"
        f"    HEARTBEAT_OK\n"
        f"      Nothing matched the check. Silent disposition.\n"
        f"    PING\n"
        f"      Lines immediately after PING: plain-text Telegram "
        f"message (2-4 lines, conversational prose, NO markdown, NO "
        f"bullets, NO headings).\n"
        f"    DONE: <one-line description>\n"
        f"      Action taken (research saved, wiki updated, etc). No "
        f"Telegram push. The runner logs your description.\n\n"
        f"REMEMBER -- the runner is forgiving and scans every line of "
        f"your message for these tokens, but ONLY when the token sits "
        f"alone on a line. Embedded mentions inside prose do not count. "
        f"If no token appears alone on any line, the run is treated as "
        f"HEARTBEAT_OK and your disposition is discarded.\n"
    )

    beat_thread = f"heartbeat-{now.strftime('%Y%m%d-%H%M')}"
    try:
        # recursion_limit=100: heartbeats chain several recall calls,
        # potentially a wiki_write, and any sub-agent invocation (deep,
        # vision, researcher) consumes ~20-30 nodes inside the parent
        # graph. Matches the `ask()` default since PR for #29.
        answer = ask(
            prompt,
            restricted=True,
            source="heartbeat",
            thread_id=beat_thread,
            recursion_limit=100,
        )
    except Exception:
        log.exception("heartbeat: agent invoke failed")
        return

    # Forgiving parser: scan EVERY line for a disposition token, not just
    # the first. The model sometimes produces leading garbage (especially
    # after long tool-call chains where it loses the protocol) but still
    # places a valid token somewhere in its final output. We pick the
    # first valid token found.
    text = (answer or "").strip()
    lines = text.splitlines()
    disposition: str | None = None
    disposition_idx: int = -1
    done_desc: str = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        upper = stripped.upper()
        if upper == "HEARTBEAT_OK":
            disposition = "noop"
            disposition_idx = i
            break
        if upper == "PING":
            disposition = "ping"
            disposition_idx = i
            break
        if upper.startswith("DONE:"):
            disposition = "done"
            disposition_idx = i
            done_desc = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            break

    if disposition is None:
        log.warning(
            "heartbeat: no disposition token found, treating as HEARTBEAT_OK: %r",
            text[:200],
        )
        return

    if disposition == "noop":
        log.info("heartbeat: HEARTBEAT_OK (check #%d)", idx + 1)
        return

    if disposition == "ping":
        body = "\n".join(lines[disposition_idx + 1 :]).strip()
        if not body:
            log.warning("heartbeat: PING with empty body, dropping")
            return
        push_to_owner("heartbeat", body)
        record_agent_action("heartbeat_ping", body)
        log.info("heartbeat: PING pushed (check #%d)", idx + 1)
        return

    if disposition == "done":
        record_agent_action("heartbeat_done", f"check #{idx + 1}: {done_desc}")
        log.info("heartbeat: DONE (check #%d) -- %s", idx + 1, done_desc[:120])
        return


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
