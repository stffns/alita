# Changelog

All notable changes to ALITA / Pelops are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-05-16

Initial cut. ALITA (Autonomous Listener, Investigator, Thinker, Aide) is the
shell; Pelops is the dog-shaped agent inside, inspired by Pelops II from
Godzilla: Singular Point.

### Added

#### Core
- Agent built on deepagents (LangGraph) with mode-detecting persona
  (thinking / fact / task).
- pydantic-settings for type-safe configuration with `SecretStr` for keys.
- Chat tools: `now`, `vstash_recall`, `vstash_remember`, `research`,
  `fetch_rss`, `followup`, `watcher`, `metrics_summary`.
- `researcher` sub-agent for multi-source briefs (delegated via `task`).
- Skills directory (`pelops/skills/`) auto-discovered at startup:
  `briefing-builder`, `paper-summarizer`.

#### Memory (vstash)
- Disciplined layers: `user-fact`, `research`, `briefing`, `consolidated`,
  `rss`, `agent-action`, `thoughts`, `episodic`, `session-state`.
- Every chat turn auto-saved to `episodic` for continuous recall.
- Session snapshot every 30 min into `session-state` for cross-session
  continuity.
- `LoggingSummarization` middleware records context-compression events.

#### Autonomy
- Scheduler with cron jobs (ingest, briefing, consolidate, pulse, health) +
  followup poller (15s) + watcher poller (60s) + session snapshot (30 min).
- Self-scheduling via cross-process SQLite (`pelops_followups`) with
  retry backoff (30s/1m/5m/15m/60m) and recursion guard.
- Watchers with event-driven change detection AND adaptive cadence
  (doubles interval after 5 NOOPs, halves on real change).
- Daily proactive pulse at 09:00 UTC.

#### Transport
- Telegram bot (aiogram), owner-restricted, with `/start /briefing /memory`
  commands.
- Chainlit web UI with action-button inbox catch-up and 15s live polling.

#### Operations
- Local metrics: tokens, cost (per model), latency, tool usage.
- Pricing table covers Groq + OpenRouter (Anthropic, OpenAI, DeepSeek).
- launchd plists for production-mode operation.

### Decisions worth noting
- Chat model: `deepseek/deepseek-v4-flash` via OpenRouter for ~30x cost
  savings over Claude Sonnet, with comparable thinking-partner quality.
- Cross-process scheduling uses a custom SQLite table rather than
  APScheduler's persistent jobstore (the latter caused executor-pool
  corruption with multiple Scheduler instances against one store).
- LoggingSummarization opt-in via `middleware=[...]` rather than
  HarnessProfile exclusion, because the default deepagents 0.6 base stack
  does NOT include SummarizationMiddleware by default.
