# ALITA

**A**utonomous **L**istener, **I**nvestigator, **T**hinker, **A**ide.

A thinking-partner agent named **Pelops** -- a dog-shaped AI inspired by Pelops II
from *Godzilla: Singular Point*. Lives in your laptop. Reads things for you,
remembers them, pushes back on weak ideas, surfaces what changed in the world,
and pings you when something genuinely worth interrupting you happens.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#)

## Stack

- **LLM**: OpenRouter (`deepseek/deepseek-v4-flash` for chat, configurable) + Groq Compound for agentic research
- **Orchestration**: [deepagents](https://github.com/langchain-ai/deepagents) (LangGraph harness)
- **Memory**: [vstash-local](../vstash-local) (local SQLite + embeddings) with 9 disciplined layers
- **Config**: pydantic-settings (typed, validated, `SecretStr` for keys)
- **Scheduler**: APScheduler in-process + custom SQLite job table for cross-process safety
- **Transport**: Telegram (push) + Chainlit (web chat with live polling inbox)
- **Persistence**: launchd plists for production-mode on macOS

## Setup

```
cd stilt
uv venv && source .venv/bin/activate
uv pip install -e .

# macOS-only: clear the UF_HIDDEN flag on .pth files. Python 3.11+ skips
# .pth files that have this flag, and macOS sets it on everything created
# inside `.venv`. Without this step, editable installs fail silently.
chflags -R nohidden .venv

cp .env.example .env
# edit .env: set GROQ_API_KEY, customize PELOPS_TOPICS and PELOPS_RSS_FEEDS
```

Run `chflags -R nohidden .venv` again any time after `uv pip install`. The
script at `scripts/fix-pth.sh` does this for you.

## Run

```
# chat UI -- PYTHONPATH is required so chainlit can import the `pelops` package
PYTHONPATH=$(pwd) chainlit run pelops/ui.py -w

# autonomy loop (separate terminal)
python -m pelops.scheduler

# Telegram bot (separate terminal, optional)
python -m pelops.telegram_bot
```

### Telegram setup (one-time)

1. Open Telegram, talk to `@BotFather`, send `/newbot`, pick a name and
   handle. BotFather returns a token like `1234:AAFooBar...`.
2. Paste the token into `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. Run `python -m pelops.telegram_bot`. Send any message to your bot from
   Telegram. The bot logs your chat_id to the terminal:
   `TELEGRAM_OWNER_CHAT_ID not set. Your chat_id is: 123456789`.
4. Add that number to `.env` as `TELEGRAM_OWNER_CHAT_ID=123456789` and restart
   the bot. Pelops is now owner-restricted and will refuse strangers.

Once configured, the scheduler's `job_briefing` and `job_consolidate` auto-push
their output to your Telegram chat when they fire.

### Production-mode (launchd) -- always-on Pelops

To run the scheduler and Telegram bot continuously (survive reboots, sleeps,
and crashes), install the bundled launchd agents:

```
cp scripts/com.pelops.scheduler.plist ~/Library/LaunchAgents/
cp scripts/com.pelops.telegram.plist ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.pelops.scheduler.plist
launchctl load ~/Library/LaunchAgents/com.pelops.telegram.plist
```

Verify:
```
launchctl list | grep pelops
tail -f data/logs/scheduler.out data/logs/telegram.out
```

To stop:
```
launchctl unload ~/Library/LaunchAgents/com.pelops.scheduler.plist
launchctl unload ~/Library/LaunchAgents/com.pelops.telegram.plist
```

The plist files assume your project lives at
`/Users/jaysonsteffens/Desktop/Personal/Projects/stilt`. Edit them if your
path differs before copying.

Note: launchd cannot keep the laptop awake. If the laptop sleeps, scheduled
jobs that would have fired during sleep are skipped (APScheduler may fire
them late, depending on the trigger). For 24/7 autonomy, host on a VPS.

### Self-scheduling

Pelops can program itself. From any chat (Telegram or Chainlit), say
something like *"check back on this in 3 days"* or *"recordamelo cada lunes
a las 9"*. Pelops will call `schedule_followup` with a future-prompt and
the trigger, and you will receive the result via Telegram when it fires.

Jobs persist in `data/jobs.db` so they survive restarts. Use
*"que recordatorios tienes pendientes?"* to see scheduled follow-ups;
*"cancela el recordatorio fu_xxx"* to drop one.

## Architecture

```
            +---------------------+
            |    Chainlit UI      |
            +----------+----------+
                       |
            +----------v----------+
            |  deepagents core    |
            |  persona = Pelops   |
            +----+------------+---+
                 |            |
        +--------v---+    +---v------------+
        | chat model |    | tools          |
        | qwen-3-32b |    |  vstash_recall |
        | (groq)     |    |  vstash_remem  |
        +------------+    |  research()    |
                          |  fetch_rss()   |
                          +---+------------+
                              |
                       +------v----------+
                       | groq/compound   |
                       | (web + visit +  |
                       |  code exec)     |
                       +-----------------+

  scheduler (APScheduler, separate process)
    - ingest      */6 h    pull RSS feeds -> memory
    - briefing    08:00    summarize last 24h
    - consolidate 03:00    distill episodic -> semantic
    - health      */15 m   heartbeat
```

## Environment variables

| Var | Default | What |
|---|---|---|
| `GROQ_API_KEY` | (required) | https://console.groq.com/keys |
| `PELOPS_CHAT_MODEL` | `groq:qwen/qwen3-32b` | The conversational model |
| `PELOPS_RESEARCH_MODEL` | `groq/compound` | Deep agentic research |
| `PELOPS_RESEARCH_MODEL_FAST` | `groq/compound-mini` | Quick web lookup |
| `PELOPS_VSTASH_PROJECT` | `pelops` | vstash project tag |
| `PELOPS_VSTASH_DB` | `./data/pelops.db` | vstash SQLite path |
| `PELOPS_OWNER` | `friend` | Used in the persona |
| `PELOPS_TOPICS` | (empty) | CSV of focus areas |
| `PELOPS_RSS_FEEDS` | (empty) | CSV of feed URLs to ingest |
| `PELOPS_BRIEFING_CRON` | `0 8 * * *` | When to send morning briefing |
| `PELOPS_CONSOLIDATE_CRON` | `0 3 * * *` | When to distill notes |
| `PELOPS_INGEST_CRON` | `0 */6 * * *` | When to scan feeds |

## Tools available to the agent

- `vstash_recall(query, top_k)` -- semantic search in long-term memory
- `vstash_remember(content, title, tags)` -- store a note
- `research(query, deep=False)` -- web research via Groq Compound
- `fetch_rss(feed_url, limit)` -- pull latest entries from an RSS feed

Plus deepagents built-ins: `write_todos`, filesystem (`read_file`, `write_file`,
`edit_file`, `ls`, `glob`, `grep`), `task` for sub-agents, and context compaction.

### Sub-agents

The parent agent can delegate to specialized sub-agents through the built-in
`task` tool. Currently shipped:

- **`researcher`** -- owns `research` + `vstash_remember`. Given a research
  question, returns a structured markdown brief (TL;DR + Findings + Sources)
  and optionally saves it to vstash. Use for anything that needs multiple
  sources or a citable brief.

Add more in `pelops/subagents.py` -- they show up in the parent agent's `task`
tool automatically.

deepagents also supports **async sub-agents** (running on a remote LangGraph
server, non-blocking). Today Pelops does not need them; see
[docs/async-subagents.md](docs/async-subagents.md) for when and how to migrate.

### Skills (progressive disclosure)

`pelops/skills/<name>/SKILL.md` files are auto-discovered at startup. The agent
sees only the YAML frontmatter (`name`, `description`) and decides on its own
when to read the full file via `read_file`. This keeps the system prompt small
and lets you grow Pelops's capabilities without code changes.

Currently shipped:

- **`briefing-builder`** -- workflow for assembling a daily briefing from the
  last 24h of vstash material.
- **`paper-summarizer`** -- workflow for turning an arXiv URL into a
  one-screen technical summary.

To add a skill, drop a new directory under `pelops/skills/` with a `SKILL.md`
that starts with YAML frontmatter:

```yaml
---
name: my-skill
description: When to use this skill. The agent reads this to decide.
---

# My Skill

## Workflow
1. step one
2. step two
...
```

No code changes required.

### When does Pelops save to vstash?

Two paths:

1. **Deterministic (scheduler)** -- `pelops/scheduler.py` writes RSS ingest
   results on a cron schedule. No model decision involved.
2. **Model-decided (chat)** -- The LLM calls `vstash_remember` when the
   persona prompt's rules tell it to: "save the synthesis after a non-trivial
   research task". This is best-effort. To force a save, ask explicitly
   ("guardalo en memoria").

For the writes that go through tools, embeddings are computed locally with
FastEmbed (no network round-trip), and the SQLite database lives at the path
in `PELOPS_VSTASH_DB`.

## Cost (rough, for moderate use)

- Chat (~100 msgs/day, `qwen-3-32b`): ~$0.30/mo
- Daily ingest + briefing + consolidation: ~$3-5/mo
- Compound tool fees (web search $0.005/call, visit $0.001/call): ~$1-2/mo
- **Total: ~$5-10/mo**

## Next steps

1. Add a Telegram bot transport so Pelops can ping you on briefings.
2. Add a `skills/` directory (deepagents 0.6+ scans it) so Pelops can grow new
   capabilities without code changes.
3. Move scheduler to Temporal once the loop needs durable retries.
