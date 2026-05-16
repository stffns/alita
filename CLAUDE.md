# CLAUDE.md

Project-level instructions for Claude Code. Loaded into context every session, so this file pays its rent in signal-per-token: short, imperative, pointer-based.

## What this is

**ALITA** (Autonomous Listener, Investigator, Thinker, Aide) is a personal research-companion agent. The agent inside is named **Pelops** (after Pelops II from *Godzilla: Singular Point*). Single author, runs on a laptop. Not a product, not a library -- a system Jay actually uses.

Stack pillars:
- Agent harness: [deepagents](https://github.com/langchain-ai/deepagents) 0.6 (LangGraph underneath)
- Chat model: `openrouter:deepseek/deepseek-v4-flash` (configurable via `PELOPS_CHAT_MODEL`)
- Memory: [`vstash-local`](../vstash-local) (SQLite + embeddings, layered taxonomy)
- Scheduler: APScheduler in-memory + custom SQLite table for cross-process follow-ups
- Transport: Telegram bot (aiogram) + Chainlit web UI

For the durable decisions read `docs/adr/`. Each ADR is one page.

## Where things live

```
pelops/
  agent.py            # build_agent() factory, ask() entry point
  persona.py          # the system prompt -- thinking-partner mode rules live here
  config.py           # pydantic-settings; SecretStr for keys
  tools.py            # all chat tools the agent can call (now/vstash_*/research/...)
  subagents.py        # researcher sub-agent declaration
  scheduler.py        # cron jobs (briefing/pulse/ingest/consolidate) + pollers
  jobs.py             # pelops_followups table -- cross-process scheduling
  watchers.py         # pelops_watchers table -- event-driven change detection
  migrations.py       # forward-only SQL; ALL schema changes go here
  metrics.py          # tokens/cost/latency per turn + tool usage
  callbacks.py        # MetricsCallback -- attached to every agent.invoke()
  middleware.py       # LoggingSummarization (compression visibility)
  logging_config.py   # JSON-lines rotating file + plain console
  doctor.py           # `pelops-doctor` operational health checks
  telegram_bot.py     # owner-restricted aiogram bot; entry point
  ui.py               # Chainlit chat
  memory.py           # vstash Memory singleton accessor
  autoschedule.py     # APScheduler singleton + persistent jobstore wiring
  skills/             # file-based skills (auto-discovered)
tests/unit/           # hermetic pytest; no real APIs, tmp_db fixture for DB
docs/adr/             # architecture decision records (read for "why")
scripts/              # install-launchd.sh, smoke/, plists, fix-pth.sh
```

## Conventions that matter (non-obvious)

**Memory layers are sacred.** Every `vstash_remember` call MUST pick a `layer` from the taxonomy in `pelops/tools.py` (`LAYER_*` constants). Mixing layers makes recall hallucinate Jay's project from RSS items. See ADR-0003.

**Schema changes go through `pelops/migrations.py`.** Add a new `(version, label, sql)` tuple to `MIGRATIONS`, never another `ALTER TABLE try/except` block. `migrate()` is tolerant of older DBs but the contract is "all DDL lives in one ordered list".

**Secrets via `SecretStr`.** Anything sensitive in `pelops/config.py` is `SecretStr`; callers use `.get_secret_value()` only at the boundary (API client construction). Never log, repr, or pass raw to format strings.

**The persona is in `pelops/persona.py`, nowhere else.** Don't embed agent instructions in tool docstrings or scheduler prompts. Tool docstrings describe what the tool does; persona owns behavior.

**Tools follow action-style for multi-verb operations.** `followup(action="schedule|list|cancel", ...)` and `watcher(action="schedule|list|cancel", ...)`. Don't add three separate tools when one would do -- shrinks the prompt and matches the Hermes idiom.

**Followup execution runs `ask(restricted=True)`.** This drops `followup` from the toolset so a fired follow-up cannot recursively schedule more. Recursion guard. If you add new "scheduling-like" tools, decide whether they belong on the restricted list too.

**Tests are hermetic.** Use the `tmp_db` fixture in `tests/conftest.py`. Do not hit real APIs, real vstash, or real Telegram. Modules requiring vstash are NOT tested in CI by design -- vstash-local is not installable on the runner.

**`pelops/__init__.py` MUST NOT eagerly import `pelops.agent`.** That triggers `pelops.tools` -> `vstash` at import time and breaks CI. Callers do `from pelops.agent import build_agent` explicitly.

## How to verify changes

```bash
# Lint + format + tests (what CI runs)
ruff check pelops tests
ruff format --check pelops tests
python -m pytest -q

# Operational health (against your live laptop, not CI)
pelops-doctor
```

A change is "done" when ruff is silent, pytest is green, and -- for behavior changes -- `pelops-doctor` still reports 8/8.

## Don't do this (lessons earned)

- Don't run `python -m pelops.scheduler` AND the embedded bot scheduler together against the same persistent jobstore. APScheduler executor pool gets corrupted. The bot owns the scheduler now (ADR-0002).
- Don't add features to `persona.py` past ~5k tokens without measuring. Bigger persona = more reverting to "helpful AI" defaults on small models.
- Don't put RSS feeds in `layer='user-fact'`. The `job_ingest` cron is supposed to use `layer='rss'`; if you wire a new ingest job, double check.
- Don't add custom middleware that conflicts with deepagents' base stack without checking `agent.nodes` -- the documented base stack in deepagents 0.6 does NOT actually include `SummarizationMiddleware` (we add it ourselves).
- Don't bypass the pydantic Settings -- direct `os.getenv` access leaks the validation we paid for.

## When Claude is unsure

1. Read the relevant ADR in `docs/adr/`.
2. Read the tests in `tests/unit/` -- they document the contract.
3. If the question is "is this code still right?", run `pelops-doctor` and check the live process state.
4. If you're about to add a new dependency, check whether it would break the CI install (which excludes `vstash-local` deliberately).

## Project commands (when relevant)

```bash
pelops-doctor          # 8 health checks, exits 0 if all green
pelops-bot             # start Telegram bot + embedded scheduler
pelops-scheduler       # standalone scheduler (legacy; bot already runs it)
bash scripts/install-launchd.sh           # install autostart
bash scripts/install-launchd.sh --uninstall
```

For everything else, prefer reading `pyproject.toml` over recalling commands from memory -- entry points, dev deps, and tool config all live there.
