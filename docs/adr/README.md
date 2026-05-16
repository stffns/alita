# Architecture Decision Records

Short notes on the durable choices behind Pelops. Each ADR is one
decision, one page, one moment in time. Read them when:

- you forget WHY something is the way it is,
- you are about to undo something and want to know what you would lose,
- you are onboarding someone new.

| # | Title | Status |
|---|---|---|
| [0001](0001-deepagents-as-harness.md) | deepagents as the agent harness | Accepted |
| [0002](0002-cross-process-scheduling.md) | Cross-process scheduling via custom SQLite table | Accepted |
| [0003](0003-memory-layer-taxonomy.md) | vstash memory layer taxonomy | Accepted |
| [0004](0004-chat-model-deepseek.md) | Chat model = deepseek-v4-flash via OpenRouter | Accepted |

When adding a new one:

1. Copy `0000-template.md` to the next number.
2. Use kebab-case title.
3. Keep it under one screen. If it sprawls, the decision is two
   decisions in a trenchcoat.
