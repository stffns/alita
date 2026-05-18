"""Tools exposed to the Pelops agent.

Three groups:
  - memory: persistent recall via vstash
  - research: web search + page extraction via groq/compound
  - ingest: pull RSS or a single URL into memory
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import feedparser
from groq import Groq
from langchain_core.tools import tool
from vstash.ingest import ingest_text

from pelops import jobs, watchers
from pelops.config import Settings
from pelops.memory import get_memory

# Layer taxonomy. Every note in vstash MUST be tagged with exactly one of
# these. The agent uses them to filter recall and avoid conflating sources.
LAYER_USER = "user-fact"  # things Jay personally told Pelops about himself
LAYER_RESEARCH = "research"  # syntheses produced by the researcher sub-agent
LAYER_BRIEFING = "briefing"  # morning briefings the scheduler produced
LAYER_CONSOLIDATED = "consolidated"  # semantic notes the nightly consolidator produced
LAYER_RSS = "rss"  # raw RSS ingest dumps (rarely worth recalling directly)
LAYER_AGENT_ACTION = "agent-action"  # things Pelops has pushed to Jay (own history)
LAYER_THOUGHTS = "thoughts"  # Pelops's own evolving curiosity, hypotheses, questions
LAYER_EPISODIC = "episodic"  # raw chat turns (user msg + response) for continuous recall
LAYER_SESSION_STATE = (
    "session-state"  # rolling summary of recent activity for cross-session continuity
)
ALL_LAYERS = (
    LAYER_USER,
    LAYER_RESEARCH,
    LAYER_BRIEFING,
    LAYER_CONSOLIDATED,
    LAYER_RSS,
    LAYER_AGENT_ACTION,
    LAYER_THOUGHTS,
    LAYER_EPISODIC,
    LAYER_SESSION_STATE,
)


def record_chat_turn(user_msg: str, response: str, source: str = "chat") -> None:
    """Save a chat exchange to vstash for continuous episodic recall.

    Builds a single note per turn so future Pelops can do:
        vstash_recall(query='...', layer='episodic')
    and surface what was actually said. This is the missing piece between
    "in-session context" (short, ephemeral) and "user-fact" (curated). It
    is the raw river of conversation.
    """
    import logging

    log = logging.getLogger("pelops.tools.record_chat_turn")
    if not user_msg or not response:
        return
    # Skip trivial turns to keep recall signal-dense.
    if len(user_msg.strip()) < 4 and len(response.strip()) < 20:
        return
    try:
        from datetime import datetime

        from vstash.ingest import ingest_text

        mem = get_memory()
        s = Settings.load()
        ts = datetime.now(UTC)
        title = f"chat_{source}_{ts.strftime('%Y%m%d_%H%M%S')}"
        body = (
            f"# Chat turn ({source})\n"
            f"Date: {ts.isoformat()}\n\n"
            f"## Jay\n{user_msg}\n\n"
            f"## Pelops\n{response}"
        )
        ingest_text(
            text=body,
            title=title,
            cfg=mem._cfg,
            store=mem._store,
            project=s.vstash_project,
            layer=LAYER_EPISODIC,
            tags=f"episodic,{source}",
        )
    except Exception as exc:
        log.warning("record_chat_turn failed: %s", exc)


def record_agent_action(label: str, content: str) -> None:
    """Save a record of an autonomous push Pelops just made.

    This is the "agent-action" memory layer -- it lets future-you (in a
    cron job, watcher alert, or chat turn) see what you have already told
    Jay so you do not repeat yourself.

    Safe to call from any process. Failures are logged and swallowed --
    a memory miss must never block a push.
    """
    import logging

    log = logging.getLogger("pelops.tools.record_agent_action")
    try:
        from datetime import datetime

        from vstash.ingest import ingest_text

        mem = get_memory()
        s = Settings.load()
        title = f"action_{label}_{datetime.now(UTC).strftime('%Y%m%d_%H%M')}"
        ingest_text(
            text=content,
            title=title,
            cfg=mem._cfg,
            store=mem._store,
            project=s.vstash_project,
            layer=LAYER_AGENT_ACTION,
            tags=f"action,{label}",
        )
    except Exception as exc:
        log.warning("record_agent_action failed: %s", exc)


def _groq() -> Groq:
    return Groq(api_key=Settings.load().groq_api_key.get_secret_value())


@tool
def now(tz: str = "UTC") -> str:
    """Return the current absolute time. Use this whenever you need to ground
    yourself before computing dates, scheduling follow-ups, or referencing
    "today/tomorrow".

    Args:
        tz: Timezone for the returned timestamp. Defaults to UTC. Accepted:
            'UTC', 'CEST', 'CET', or any IANA name like 'Europe/Berlin'.

    Returns:
        A string like '2026-05-14T17:42:18+00:00 (UTC, day=Thursday)'.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    aliases = {"CEST": "Europe/Berlin", "CET": "Europe/Berlin", "UTC": "UTC"}
    name = aliases.get(tz.upper(), tz)
    try:
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return f"(unknown timezone {tz!r}; try 'UTC' or 'Europe/Berlin')"
    t = datetime.now(zone)
    return f"{t.isoformat(timespec='seconds')} ({tz}, day={t.strftime('%A')})"


@tool
def vstash_recall(
    query: str,
    layer: str | None = None,
    top_k: int = 5,
    max_chars_per_result: int = 2500,
    exclude_title_prefix: str | None = None,
) -> str:
    """Search Pelops's long-term memory for relevant past notes.

    Use this BEFORE answering questions about anything Jay has discussed before,
    or anything Pelops has previously researched and stored.

    Args:
        query: natural-language query.
        layer: filter by layer. Use to AVOID CONFLATING SOURCES:
            - 'user-fact'    -> only things Jay said about himself
            - 'research'     -> only research syntheses Pelops produced
            - 'briefing'     -> only morning briefings
            - 'consolidated' -> only nightly-consolidated semantic notes
            - 'rss'          -> raw RSS ingest dumps
            Omit to search across all layers (only do this for broad
            exploratory queries; for "what do you know about Jay?" pass
            layer='user-fact').
        top_k: number of results AFTER filtering (the search internally
            fetches a wider candidate set so an `exclude_title_prefix`
            doesn't reduce the returned count).
        max_chars_per_result: cap on chars returned per chunk. Default 2500
            balances cost against keeping enough context for the chat
            agent to recognize a note. Before this cap, chunks ran 9000+
            chars and dominated heartbeat token cost. Pass a higher value
            (or None) for queries where the full note matters.
        exclude_title_prefix: skip results whose title starts with this
            prefix. Used by the heartbeat to filter
            `action_context-compression_*` notes out of `agent-action`
            recalls -- those are bulky and represent past compression
            events, not genuine new activity.
    """
    # Over-fetch when a filter is in play so we can still return `top_k`
    # after dropping matches. Gemini caught this: the previous code
    # would return < top_k whenever the filter ate hits.
    fetch_k = top_k * 3 if exclude_title_prefix else top_k
    results = get_memory().search(query, top_k=fetch_k, layer=layer)
    if not results:
        return "(no relevant memories found)"
    lines = []
    for r in results:
        if len(lines) >= top_k:
            break
        title = getattr(r, "title", "") or ""
        if exclude_title_prefix and title.startswith(exclude_title_prefix):
            continue
        score = getattr(r, "score", None)
        text = getattr(r, "text", None) or getattr(r, "content", "") or str(r)
        if max_chars_per_result and len(text) > max_chars_per_result:
            text = (
                text[:max_chars_per_result] + f"\n... [truncated, full chunk was {len(text)} chars]"
            )
        source = getattr(r, "source", "") or getattr(r, "path", "")
        idx = len(lines) + 1
        lines.append(f"[{idx}] score={score:.3f} source={source}\n{text}\n")
    if not lines:
        return "(no relevant memories after filter)"
    return "\n".join(lines)


@tool
def vstash_remember(
    content: str,
    title: str | None = None,
    layer: str = LAYER_USER,
    tags: str | None = None,
) -> str:
    """Save a new note into Pelops's long-term memory.

    Args:
        content: the note to save.
        title: short kebab-case-ish slug (used to retrieve later).
        layer: REQUIRED. Pick ONE of:
            - 'user-fact'    -> things Jay said about himself or his preferences
            - 'research'     -> a synthesis of research Pelops did for Jay
            - 'briefing'     -> a daily briefing produced by the scheduler
            - 'consolidated' -> a semantic note distilled from many episodic ones
            - 'rss'          -> raw RSS ingest dump
            Mixing layers makes future recall fuzzy. Be deliberate.
        tags: optional comma-separated free-form tags. Layer is the
            primary filter; tags are for free-form annotation.
    """
    if layer not in ALL_LAYERS:
        return f"Error: layer={layer!r} is not one of {ALL_LAYERS}. Pick exactly one and re-call."
    mem = get_memory()
    s = Settings.load()
    result = ingest_text(
        text=content,
        title=title,
        cfg=mem._cfg,
        store=mem._store,
        project=s.vstash_project,
        layer=layer,
        tags=tags,
    )
    chunks = getattr(result, "chunks", None) or getattr(result, "chunk_count", "?")
    return f"Stored {chunks} chunk(s) as '{title or 'auto'}'."


@tool
def research(query: str, deep: bool = False) -> str:
    """Investigate a topic on the open web via Groq Compound.

    Compound autonomously runs web search + visit + (optionally) code execution
    server-side and returns a synthesized answer. Use this for anything that
    requires fresh information, multiple sources, or fact-checking.

    Args:
        query: The research question, written as a clear natural-language prompt.
        deep: If True, uses groq/compound (up to 10 tool calls, slower, more thorough).
              If False, uses groq/compound-mini (1 tool call, ~3x faster).
    """
    s = Settings.load()
    model = s.research_model if deep else s.research_model_fast
    resp = _groq().chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": query}],
    )
    return resp.choices[0].message.content or "(empty response)"


@tool
def fetch_rss(feed_url: str, limit: int = 5) -> str:
    """Fetch the latest N entries from an RSS/Atom feed.

    Returns a compact summary (title + link + published + summary). Useful when
    Pelops wants to scan a known feed before deciding what to research deeply.
    """
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        return f"(failed to parse feed: {feed_url})"
    entries = parsed.entries[:limit]
    lines = [f"# {parsed.feed.get('title', feed_url)}"]
    for e in entries:
        title = e.get("title", "(untitled)")
        link = e.get("link", "")
        published = e.get("published", "")
        summary = (e.get("summary", "") or "")[:300]
        lines.append(f"\n## {title}\n{link}\n{published}\n{summary}")
    return "\n".join(lines)


@tool
def followup(
    action: str,
    prompt: str | None = None,
    when: str | None = None,
    label: str = "followup",
    job_id: str | None = None,
) -> str:
    """Manage scheduled follow-ups -- single tool with action-style operations.

    Use this whenever a conversation implies a future action, OR when the
    user asks you to list or cancel previously scheduled follow-ups.

    Args:
        action: One of "schedule", "list", or "cancel".

        prompt: (action="schedule" only) The instruction you will receive
            when the trigger fires. Write it AS IF you are sending a message
            to your future self -- include enough context that the future
            invocation knows what to do.

        when: (action="schedule" only) When to fire. PREFER relative offsets
            over absolute dates -- you are bad at date arithmetic.

            Accepted forms (in this order of preference):
            1. Relative offset (BEST for short waits):
                'in 2 minutes', 'in 3 hours', 'in 1 day', 'in 2 weeks'
                The server computes the absolute time from "now". Use this
                whenever the user says "in N units", "tomorrow", etc.
            2. 5-field cron (for repeating jobs):
                '0 9 * * *'    -- daily at 09:00 UTC
                '0 9 * * mon'  -- mondays at 09:00 UTC
            3. ISO datetime in UTC (only when you have a SPECIFIC absolute time):
                '2026-05-15T09:00:00'

            Do NOT compute ISO datetimes from natural language. If unsure,
            use the relative form.

        label: (action="schedule" only) Short slug to identify the job.

        job_id: (action="cancel" only) The id returned when the job was
            originally scheduled.

    Returns:
        - action="schedule" -> "Scheduled. job_id=fu_xxx when=..."
        - action="list" -> a newline-separated list of pending jobs
        - action="cancel" -> "Cancelled <id>." or "No active job named <id>."
    """
    action = (action or "").strip().lower()
    if action == "schedule":
        if not prompt or not when:
            return "Error: action='schedule' requires both `prompt` and `when`."
        try:
            new_id = jobs.add(prompt=prompt, when=when, label=label)
        except ValueError as exc:
            return (
                f"Error: could not schedule. {exc}. "
                f"Tip: for compound offsets use 'in 15 hours and 29 minutes'; "
                f"for absolute times use ISO 'YYYY-MM-DDTHH:MM:SS' in UTC "
                f"(use the `now` tool to ground yourself first)."
            )
        return f"Scheduled. job_id={new_id} when={when}"
    if action == "list":
        pending = jobs.list_pending()
        if not pending:
            return "(no scheduled follow-ups)"
        return "\n".join(
            f"- {j['id']}  next={j['run_at_utc']}  cron={j['cron'] or '-'}" for j in pending
        )
    if action == "cancel":
        if not job_id:
            return "Error: action='cancel' requires `job_id`."
        ok = jobs.cancel(job_id)
        return f"Cancelled {job_id}." if ok else f"No active job named {job_id}."
    return f"Error: unknown action {action!r}. Use schedule | list | cancel."


@tool
def watcher(
    action: str,
    query: str | None = None,
    interval: str | None = None,
    label: str = "watch",
    watcher_id: str | None = None,
) -> str:
    """Watch a research query for changes. When the answer to the query
    meaningfully changes, Pelops fires an autonomous alert.

    This is what makes Pelops event-driven instead of clock-driven. Use it
    when the user says: "vigila X", "avisame si Y cambia", "estate pendiente
    de Z", "watch for new releases of A", etc.

    Args:
        action: "schedule", "list", or "cancel".

        query: (action="schedule" only) The research query to re-run on the
            interval. Write it like a focused web search question, e.g.
            "Has Anthropic released a new model in the last 24h?"

        interval: (action="schedule" only) How often to re-check. Forms:
            - "every 30 minutes", "every 2 hours", "every 1 day"
            - integer of seconds (minimum is 60)
            Pick an interval that matches the topic's pace. Daily news
            sources -> every 6h or every 1 day. Arxiv -> every 1 day.
            Anything more frequent than every 30 minutes is wasteful.

        label: (action="schedule" only) Short slug for log/list output.

        watcher_id: (action="cancel" only) The id returned at schedule time.

    Returns:
        - schedule -> "Watching. watcher_id=w_xxx every=Ns"
        - list -> newline-separated watcher rows
        - cancel -> "Stopped <id>." or "No active watcher named <id>."
    """
    action = (action or "").strip().lower()
    if action == "schedule":
        if not query or not interval:
            return "Error: action='schedule' requires `query` and `interval`."
        try:
            wid = watchers.add(query=query, interval=interval, label=label)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Watching. watcher_id={wid} every={interval}"
    if action == "list":
        active = watchers.list_active()
        if not active:
            return "(no active watchers)"
        return "\n".join(
            f"- {w['id']}  every={w['interval_seconds']}s  "
            f"last_checked={w['last_checked_at'] or 'never'}  query={w['query'][:60]!r}"
            for w in active
        )
    if action == "cancel":
        if not watcher_id:
            return "Error: action='cancel' requires `watcher_id`."
        ok = watchers.cancel(watcher_id)
        return f"Stopped {watcher_id}." if ok else f"No active watcher named {watcher_id}."
    return f"Error: unknown action {action!r}. Use schedule | list | cancel."


@tool
def metrics_summary(hours: int = 24) -> str:
    """Show Pelops's recent activity: LLM turns, tokens, cost, tool usage.

    Use this when the user asks "what have you been doing", "what is this
    costing", "which tool is slow", or any operational question. Returns a
    short plain-text summary covering the last `hours` (default 24).
    """
    from pelops import metrics

    return metrics.format_summary(metrics.summary(hours=hours))


# ----- Wiki tools -----------------------------------------------------------
#
# These are EXPOSED but NOT yet registered in CHAT_TOOLS. Wiring into the
# agent happens in Phase 2 of the wiki rollout (see docs/wiki-plan.md). For
# now the tools exist so they can be unit-tested in isolation and adopted
# deliberately once the heartbeat experiment validates.


@tool
def wiki_read(slug: str) -> str:
    """Read a wiki page by its slug.

    The wiki is Alita's compiled-knowledge layer -- mutable markdown pages
    with `[[backlinks]]` answering "what do I know about X". Use this when
    the user asks "que sabes de X" or "que entiendes sobre Y", as opposed
    to `vstash_recall` which surfaces raw source material.

    Args:
        slug: Kebab-case page identifier (e.g., "deepagents", "heartbeat").

    Returns:
        The full markdown of the page including frontmatter, or a clear
        "not found" message if no such page exists.
    """
    from pelops import wiki

    try:
        page = wiki.read(slug)
    except wiki.WikiError as exc:
        return f"Error: {exc}"
    if page is None:
        return f"No wiki page named {slug!r}. Use `wiki_list` to see what exists."
    return page.render()


@tool
def wiki_write(slug: str, body: str, sources: list[str] | None = None) -> str:
    """Create or update a wiki page.

    The wiki is YOUR compiled understanding, not a copy of sources. Each
    page is ONE canonical entry per concept. Updates are encouraged --
    when new sources contradict or extend a page, EDIT the page; do NOT
    add a parallel one.

    Anti-orphan rule: when creating a NEW page, the body must reference
    at least one EXISTING page via `[[other-slug]]`. This keeps the graph
    connected. Updates to existing pages skip this check.

    Args:
        slug: Kebab-case page identifier. Lowercase letters, digits, hyphens.
        body: Markdown body (no frontmatter -- the tool writes it for you).
            Use `[[other-slug]]` to link to other pages.
        sources: Optional list of vstash document titles or URLs that
            informed this revision. Helps later audits trace claims back.

    Returns:
        Success message with the page slug, or an error explaining what
        invariant was violated.
    """
    from pelops import wiki

    try:
        page = wiki.write(slug, body, sources=sources)
    except wiki.WikiError as exc:
        return f"Error: {exc}"
    action = "updated" if page.created != page.updated else "created"
    return f"Wiki page {slug!r} {action}."


@tool
def wiki_list() -> str:
    """List all wiki page slugs.

    Use this to discover what pages exist before deciding whether to
    create a new one or extend an existing one. Cheap -- just a directory
    listing.

    Returns:
        Newline-separated slugs in alphabetical order, or "(empty)" if no
        pages exist yet.
    """
    from pelops import wiki

    slugs = wiki.list_pages()
    if not slugs:
        return "(empty -- the wiki has no pages yet)"
    return "\n".join(slugs)


@tool
def wiki_search(query: str, limit: int = 10) -> str:
    """Substring search across wiki page bodies (case-insensitive).

    Use this when you want to know "is there already a page that mentions
    X" without listing every page. Cheap -- scans markdown files.

    Args:
        query: Phrase to look for in page bodies. Matched as a substring.
        limit: Max number of matches to return. Default 10.

    Returns:
        Lines of `slug: <snippet>` for each match, or "(no matches)" when
        nothing is found.
    """
    from pelops import wiki

    matches = wiki.search(query, limit=limit)
    if not matches:
        return "(no matches)"
    return "\n".join(f"{slug}: {snippet}" for slug, snippet in matches)


@tool
def wiki_backlinks(slug: str) -> str:
    """List wiki pages that link to a given page.

    This is the entity-graph view: "which pages mention X". Powered by
    the `[[other-slug]]` wikilink syntax. Cheap -- no LLM calls, just
    regex over markdown bodies.

    Use this when:
      - You want to know what THE WIKI itself has to say about a topic
        across many pages, not just the canonical page for that topic.
      - "que paginas hablan de X" / "donde menciono Y".
      - Before creating a new page on X: see who already mentions X
        so you can adopt the new page into the existing graph
        (anti-orphan rule).

    Args:
        slug: kebab-case page identifier to find backlinks for.

    Returns:
        Newline-separated list of "<from-slug>" lines, plus any typed
        links in the form "<from-slug> (relation)" -- e.g.,
        "alita (uses)" means alita.md has a `[[<this-slug>]] (uses)`.
        Empty list returns "(no pages link to <slug>)".
    """
    from pelops import wiki

    plain = wiki.backlinks(slug)
    typed = wiki.typed_links(slug)
    if not plain and not typed:
        return f"(no pages link to {slug!r})"
    lines = []
    # Dedupe: a typed link also appears in plain. Show typed form when
    # available; plain-only entries appear without a relation.
    typed_lookup = {from_slug: relation for from_slug, relation in typed}
    for from_slug in plain:
        if from_slug in typed_lookup:
            lines.append(f"{from_slug} ({typed_lookup[from_slug]})")
        else:
            lines.append(from_slug)
    return "\n".join(lines)


@tool
def wiki_graph_stats() -> str:
    """Summarize the wiki entity graph: top hubs, orphans, total links.

    Use sparingly -- intended for occasional introspection ("am I
    actually building a connected wiki?"), not for every query. Cheap
    (one scan, no LLM), but still walks every page.

    Returns:
        A plain-text summary with: total pages, total links, top 5 most
        referenced pages (the hubs), and any orphans (pages with zero
        inbound links).
    """
    from pelops import wiki

    graph = wiki.entity_graph()
    pages = list(graph.keys())
    if not pages:
        return "(wiki is empty)"
    total_links = sum(len(g["inbound_plain"]) + len(g["inbound_typed"]) for g in graph.values())
    by_count = sorted(
        pages,
        key=lambda s: -(len(graph[s]["inbound_plain"]) + len(graph[s]["inbound_typed"])),
    )
    top = by_count[:5]
    orphans = [s for s in pages if not graph[s]["inbound_plain"] and not graph[s]["inbound_typed"]]
    lines = [
        f"pages: {len(pages)}, total inbound links: {total_links}",
        "",
        "top hubs:",
    ]
    for s in top:
        n = len(graph[s]["inbound_plain"]) + len(graph[s]["inbound_typed"])
        lines.append(f"  {s} -- {n} inbound")
    if orphans:
        lines.append("")
        lines.append(f"orphans ({len(orphans)}):")
        for s in orphans:
            lines.append(f"  {s}")
    return "\n".join(lines)


# Wiki tools -- live in CHAT_TOOLS. The agent reads its own behavior
# from heartbeat.md via wiki_read, and may write to wiki pages (including
# heartbeat.md itself, co-editor model). Git in the vault is the safety
# net for self-edits.
WIKI_TOOLS = [
    wiki_read,
    wiki_write,
    wiki_list,
    wiki_search,
    wiki_backlinks,
    wiki_graph_stats,
]


# ---------- Skill tools (Hermes-style self-improvement) ----------------
#
# Alita can author her own skills via `skill_write`. Skills written this
# way land in `<wiki_dir>/skills/<slug>/SKILL.md` and are auto-discovered
# at the next agent build by `_discover_skill_sources()` in
# `pelops/agent.py`. Closes the closed-loop:
#   recurring task observed -> skill_write -> next ask() rebuilds -> skill loaded.
#
# Code-shipped skills in `pelops/skills/` are NOT mutable from here.
# They are the floor; vault skills extend it.

_SKILL_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _skill_paths() -> tuple[Path, Path | None]:
    """Return (code_skills_dir, vault_skills_dir_or_None).

    Code dir: `pelops/skills/`. Always present (created when needed).
    Vault dir: `<wiki_dir>/skills/`. May not exist yet; first
    `skill_write` creates it.
    """
    project_root = Path(__file__).resolve().parent.parent
    code_dir = project_root / "pelops" / "skills"
    try:
        vault_dir = Settings.load().wiki_dir / "skills"
    except Exception:
        vault_dir = None
    return code_dir, vault_dir


@tool
def skill_list() -> str:
    """List all skills available to the agent, from both code and vault.

    Returns lines of `<slug>  (<source>)` where source is `code` (shipped
    in the python package) or `vault` (Alita-authored, in the wiki).
    Sorted alphabetically with the source noted so duplicates between
    code and vault are visible if they happen.
    """
    code_dir, vault_dir = _skill_paths()
    entries: list[tuple[str, str]] = []
    if code_dir.exists():
        for d in code_dir.iterdir():
            if d.is_dir() and (d / "SKILL.md").exists():
                entries.append((d.name, "code"))
    if vault_dir and vault_dir.exists():
        for d in vault_dir.iterdir():
            if d.is_dir() and (d / "SKILL.md").exists():
                entries.append((d.name, "vault"))
    if not entries:
        return "(no skills found)"
    entries.sort()
    return "\n".join(f"{slug}  ({source})" for slug, source in entries)


@tool
def skill_read(slug: str) -> str:
    """Read a skill's SKILL.md, searching code first then vault.

    Use this before `skill_write`-ing a new skill to match the style of
    existing ones (frontmatter format, section structure, anti-pattern
    section, etc).

    Args:
        slug: kebab-case skill identifier (e.g. "wiki-page-creator").

    Returns:
        Full SKILL.md content (frontmatter + body), or a clear "not
        found" message.
    """
    if not _SKILL_SLUG_RE.match(slug):
        return f"Error: invalid slug {slug!r} (must be kebab-case)"
    code_dir, vault_dir = _skill_paths()
    # Vault wins for duplicates -- matches the load priority in
    # _discover_skill_sources(). If Alita refined a code skill in the
    # vault, that refinement is what reads back here too.
    for base, label in [(vault_dir, "vault"), (code_dir, "code")]:
        if base is None:
            continue
        path = base / slug / "SKILL.md"
        if path.exists():
            return f"[source: {label}]\n\n{path.read_text(encoding='utf-8')}"
    return f"(no skill named {slug!r} in code or vault)"


@tool
def skill_write(slug: str, body: str) -> str:
    """Create or update a skill in the vault (NOT the code repo).

    Alita uses this to author new skills when she notices a recurring
    pattern that would benefit from explicit encoding. The skill lands
    in `<wiki_dir>/skills/<slug>/SKILL.md` and is auto-discovered at
    the next agent build (which the persona-from-wiki invalidator
    triggers on file change).

    The body MUST start with a YAML frontmatter block containing
    `name:` and `description:` keys -- those are what deepagents'
    SkillsMiddleware matches against user queries. Without them the
    skill exists on disk but never auto-loads.

    Args:
        slug: kebab-case identifier; will become the directory name.
        body: full SKILL.md content including YAML frontmatter and
            the markdown body underneath.

    Returns:
        Path on success, or a clear validation error.
    """
    if not _SKILL_SLUG_RE.match(slug):
        return f"Error: invalid slug {slug!r} (must be kebab-case)"
    fm_match = _SKILL_FRONTMATTER_RE.match(body)
    if not fm_match:
        return (
            "Error: skill body must START with a YAML frontmatter block "
            "delimited by `---` lines. Required keys: `name`, "
            "`description`. Without them, deepagents' SkillsMiddleware "
            "will not match this skill against any query."
        )
    fm = fm_match.group(1)
    # Anchor to line start so a `name:` appearing INSIDE the
    # description text does not falsely satisfy the check.
    if not re.search(r"^name:", fm, re.MULTILINE) or not re.search(
        r"^description:", fm, re.MULTILINE
    ):
        return (
            "Error: frontmatter must include both `name:` and "
            "`description:` keys at the start of a line. The "
            "description is what the agent uses to decide when the "
            "skill applies."
        )
    _, vault_dir = _skill_paths()
    if vault_dir is None:
        return "Error: wiki_dir not configured (PELOPS_WIKI_DIR is empty)."
    skill_dir = vault_dir / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    tmp = skill_file.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(skill_file)
    return (
        f"Skill {slug!r} written to {skill_file}. Will be auto-loaded "
        f"on the next agent rebuild (typically next ask() turn, since "
        f"the persona-mtime invalidator clears the build_agent cache)."
    )


SKILL_TOOLS = [skill_list, skill_read, skill_write]


# ---------- Code search (Semble) ----------------------------------------


@tool
def code_search(query: str, repo: str | None = None, top_k: int = 5) -> str:
    """Search code by intent using Semble (~98% fewer tokens than grep).

    Returns ranked snippets with file path and line range. Use this
    instead of `grep` + `wiki_read` when the question is "where does
    the code do X" or "find me the implementation of Y". Cheap (CPU
    only, ~1.5ms per query after indexing).

    Args:
        query: natural-language description of the code you're looking
            for, e.g. "save model to disk", "where heartbeat parses
            markdown", "anti-orphan rule".
        repo: absolute path to the repo. Defaults to the stilt repo
            (the project this agent ships with). Pass another path
            when you want to search a different codebase.
        top_k: max snippets to return. Default 5; raise to 10 for
            broader exploration.

    Returns:
        Formatted snippets: each entry is "<file>:<start>-<end>" on
        one line, followed by the code. Empty result returns
        "(no matches)".
    """
    from pelops import code as code_mod

    try:
        results = code_mod.search(query, repo=repo, top_k=top_k)
    except Exception as exc:
        return f"Error: {exc}"
    if not results:
        return "(no matches)"
    out = []
    for r in results:
        loc = f"{r['file']}:{r['start_line']}-{r['end_line']}"
        out.append(f"=== {loc} ===\n{r['snippet']}")
    return "\n\n".join(out)


CODE_TOOLS = [code_search]


# ---------- GitHub (read-only) ------------------------------------------
#
# Deliberately READ-only. Any write capability (PR creation, commenting,
# branch push) lives in its own future module behind explicit
# authorization. See `docs/design/sandbox-execution.md` for the broader
# plan around write access and code execution.


@tool
def gh_pr_list(repo: str, state: str = "open", limit: int = 10) -> str:
    """List pull requests on a GitHub repository.

    Args:
        repo: 'owner/name' (e.g. 'stffns/alita').
        state: 'open', 'closed', 'merged', or 'all'. Default 'open'.
        limit: max PRs to return.

    Returns:
        One PR per line: "#<number> [<state>] <title> -- <author> (<url>)".
        Empty list returns "(no PRs)".
    """
    from pelops import github_read

    try:
        prs = github_read.pr_list(repo, state=state, limit=limit)
    except github_read.GitHubReadError as exc:
        return f"Error: {exc}"
    if not prs:
        return "(no PRs)"
    return "\n".join(
        f"#{p['number']} [{p.get('state', '?')}] {p.get('title', '?')} -- "
        f"{(p.get('author') or {}).get('login', '?')} ({p.get('url', '')})"
        for p in prs
    )


@tool
def gh_pr_view(repo: str, number: int) -> str:
    """Fetch one PR with body, author, line counts, comments summary."""
    from pelops import github_read

    try:
        pr = github_read.pr_view(repo, number)
    except github_read.GitHubReadError as exc:
        return f"Error: {exc}"
    author = (pr.get("author") or {}).get("login", "?")
    body = (pr.get("body") or "").strip()[:1500]
    n_comments = len(pr.get("comments") or [])
    return (
        f"#{pr.get('number')} [{pr.get('state')}] {pr.get('title')}\n"
        f"author: {author}  created: {pr.get('createdAt')}  merged: {pr.get('mergedAt')}\n"
        f"changes: +{pr.get('additions', 0)} -{pr.get('deletions', 0)} "
        f"across {pr.get('changedFiles', 0)} files\n"
        f"comments: {n_comments}\n"
        f"url: {pr.get('url')}\n\n"
        f"--- body ---\n{body}"
    )


@tool
def gh_issue_list(repo: str, state: str = "open", limit: int = 10) -> str:
    """List issues on a GitHub repository.

    Args:
        repo: 'owner/name'.
        state: 'open', 'closed', or 'all'. Default 'open'.
        limit: max issues.
    """
    from pelops import github_read

    try:
        issues = github_read.issue_list(repo, state=state, limit=limit)
    except github_read.GitHubReadError as exc:
        return f"Error: {exc}"
    if not issues:
        return "(no issues)"
    lines = []
    for i in issues:
        labels = ", ".join(label.get("name", "") for label in (i.get("labels") or []))
        author = (i.get("author") or {}).get("login", "?")
        lines.append(
            f"#{i.get('number')} [{i.get('state', '?')}] {i.get('title', '?')} -- "
            f"{author}  labels=[{labels}]  ({i.get('url', '')})"
        )
    return "\n".join(lines)


@tool
def gh_issue_view(repo: str, number: int) -> str:
    """Fetch one issue with body + comments summary."""
    from pelops import github_read

    try:
        iss = github_read.issue_view(repo, number)
    except github_read.GitHubReadError as exc:
        return f"Error: {exc}"
    author = (iss.get("author") or {}).get("login", "?")
    labels = ", ".join(label.get("name", "") for label in (iss.get("labels") or []))
    body = (iss.get("body") or "").strip()[:1500]
    n_comments = len(iss.get("comments") or [])
    return (
        f"#{iss.get('number')} [{iss.get('state')}] {iss.get('title')}\n"
        f"author: {author}  created: {iss.get('createdAt')}  labels=[{labels}]\n"
        f"comments: {n_comments}\n"
        f"url: {iss.get('url')}\n\n"
        f"--- body ---\n{body}"
    )


@tool
def gh_recent_commits(repo: str, branch: str = "main", limit: int = 10) -> str:
    """Recent commits on a branch."""
    from pelops import github_read

    try:
        commits = github_read.recent_commits(repo, branch=branch, limit=limit)
    except github_read.GitHubReadError as exc:
        return f"Error: {exc}"
    if not commits:
        return "(no commits)"
    return "\n".join(
        f"{c['sha']}  {c['date'][:10]}  {c['author']:<20s}  {c['message']}" for c in commits
    )


GITHUB_TOOLS = [gh_pr_list, gh_pr_view, gh_issue_list, gh_issue_view, gh_recent_commits]


# ---------- Sandbox code execution (Docker) -----------------------------
#
# Per Jay's decision on 2026-05-18: no per-call permission prompt. The
# sandbox isolation (no network, read-only FS, stripped env, CPU/memory
# caps -- see `pelops/sandbox.py`) is the safety boundary, so a paranoia
# layer on top would only add latency. The kill switch is the env var
# ALITA_SANDBOX_ENABLED -- set to "false" to disable the tool entirely.


@tool
def code_execute(code: str, language: str = "python", timeout_seconds: int = 30) -> str:
    """Run code in a sandboxed Docker container and return its output.

    The container has NO network, a read-only root filesystem (writable
    /tmp only), CPU + memory caps, and no host credentials. Each call
    spins up a fresh container that is destroyed on exit.

    Args:
        code: source to run. For language='python' it is fed to
            `python <script>`; for 'bash' it is fed to `sh <script>`.
        language: 'python' (default) or 'bash'. Python image is
            stdlib-only (no `pip install`); bash runs on Alpine.
        timeout_seconds: kill the container after this many seconds.
            Default 30. Hard cap 300.

    Returns:
        Multi-line summary: exit code, duration, stdout, stderr.
        Truncated to a few KB to keep the agent context lean.
    """
    from pelops import sandbox
    from pelops.config import get_settings

    if not getattr(get_settings(), "sandbox_enabled", True):
        return "Error: sandbox execution is disabled (ALITA_SANDBOX_ENABLED=false)"
    timeout = max(1, min(int(timeout_seconds), 300))
    lang = (language or "python").lower()
    try:
        if lang == "python":
            result = sandbox.run_python(code, timeout=timeout)
        elif lang in {"bash", "sh", "shell"}:
            result = sandbox.run_bash(code, timeout=timeout)
        else:
            return f"Error: unsupported language {language!r} (use 'python' or 'bash')"
    except sandbox.SandboxError as exc:
        return f"Error: {exc}"
    head = (
        f"exit={result['exit_code']}  "
        f"duration={result['duration_seconds']}s"
        f"{'  TIMED_OUT' if result['timed_out'] else ''}"
    )
    stdout = result["stdout"].rstrip()
    stderr = result["stderr"].rstrip()
    parts = [head]
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)


SANDBOX_TOOLS = [code_execute]


# ---------- Host filesystem write (allowlisted) -------------------------
#
# The deepagents built-in `write_file` tool is sandboxed under
# PROJECT_ROOT via FilesystemBackend(virtual_mode=True). That sandbox
# is good for safety but makes a request like "save it to my Desktop"
# silently fail -- the file lands at `stilt/Users/.../foo.html` which
# Jay never sees. `host_write_file` is the explicit escape hatch: a
# narrow tool that writes to the REAL filesystem, but only under the
# directories in `ALITA_HOST_WRITE_DIRS` (default Desktop / Documents
# / Downloads). See `pelops/host_fs.py` for the safety contract.


@tool
def host_write_file(path: str, content: str) -> str:
    """Save a file directly to Jay's real filesystem (not the sandbox).

    Use this when Jay says "save to my Desktop" or "put it in my
    Documents". The deepagents `write_file` tool writes inside the
    repo's virtual filesystem, which is invisible to Jay. THIS tool
    writes to the actual host disk.

    Args:
        path: Absolute path (or `~`-prefixed), e.g. '~/Desktop/foo.py'.
            Must land under an allowed root -- by default `~/Desktop`,
            `~/Documents`, `~/Downloads`. Path traversal (`..`) is
            rejected. Relative paths without `~` are rejected.
        content: File body. UTF-8.

    Returns:
        On success: "Saved to <resolved-path> (N bytes)".
        On refusal: "Error: <reason>" -- typically a path-policy issue.
    """
    from pelops import host_fs

    try:
        resolved = host_fs.write(path, content)
    except host_fs.HostFsError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: write failed -- {exc}"
    return f"Saved to {resolved} ({len(content)} bytes)"


@tool
def host_read_file(path: str) -> str:
    """Read a file from Jay's real filesystem (same allowlist as host_write_file).

    Use this to VERIFY what you wrote with `host_write_file`, or to
    read anything Jay has under `~/Desktop`, `~/Documents`,
    `~/Downloads` (or whatever `ALITA_HOST_WRITE_DIRS` is set to).
    Symmetric counterpart to `host_write_file` -- the in-repo
    `read_file` tool only sees the virtual FS rooted at the stilt
    repo and will NOT find files you saved to Desktop.

    Args:
        path: Absolute path (or `~`-prefixed). The path itself must
            live inside an allowed root.

    Returns:
        File contents (UTF-8). Truncated past ~200KB with a marker.
        On policy violation or missing/non-file target: "Error: ...".
    """
    from pelops import host_fs

    try:
        return host_fs.read(path)
    except host_fs.HostFsError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: read failed -- {exc}"


@tool
def host_ls(path: str) -> str:
    """List entries of a directory on Jay's real filesystem.

    Use this when Jay says "what's in my Desktop?" or to confirm
    `host_write_file` actually persisted a file. The in-repo `ls`
    tool only sees the deepagents virtual FS.

    Args:
        path: Absolute directory path (or `~`-prefixed). Must be
            inside an allowed root.

    Returns:
        One entry per line: "<type> <size> <name>". Dirs first,
        then files (both alphabetical). `size` shown for files only.
        On policy violation or missing dir: "Error: ...".
    """
    from pelops import host_fs

    try:
        entries = host_fs.ls(path)
    except host_fs.HostFsError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: ls failed -- {exc}"
    if not entries:
        return "(empty directory)"
    lines = []
    for e in entries:
        size = "-" if e["size"] is None else f"{e['size']:>9}"
        lines.append(f"{e['type']:<5} {size}  {e['name']}")
    return "\n".join(lines)


HOST_FS_TOOLS = [host_write_file, host_read_file, host_ls]


CHAT_TOOLS = [
    now,
    vstash_recall,
    vstash_remember,
    research,
    fetch_rss,
    followup,
    watcher,
    metrics_summary,
    *WIKI_TOOLS,
    *SKILL_TOOLS,
    *CODE_TOOLS,
    *GITHUB_TOOLS,
    *SANDBOX_TOOLS,
    *HOST_FS_TOOLS,
]
