# 4. Chat model = deepseek-v4-flash via OpenRouter

- Status: Accepted
- Date: 2026-05-15
- Deciders: Jay

## Context

The persona we want from Pelops -- detect "thinking mode" vs "fact mode",
push back instead of comply, refuse to dump a 5-section consulting analysis
when asked to be "more proactive" -- proved to be steerability-limited on
Groq's open-weight models.

We tested:

- **Qwen3-32B** on Groq: reverts to "helpful AI dump" on the second turn
  when the user uses meta-language ("be more proactive").
- **Llama-3.3-70B** on Groq: same failure mode in a different shape.
  Also tends toward corporate "I will provide value" phrasing.
- **Claude Sonnet 4.6** via OpenRouter: handles the meta-instruction
  cleanly, picks up persona rules, self-corrects without being told.
- **DeepSeek-v4-flash** via OpenRouter: matches Claude Sonnet's
  persona quality in the same test prompts, at a price point ~30x lower.

## Decision

Default chat model is **`openrouter:deepseek/deepseek-v4-flash`**
(~$0.11 in / $0.22 out per million tokens).

`groq/compound` and `groq/compound-mini` remain the research-tool path
(server-side web search). Groq's open-weight models stay configurable
for users who want them but are not the default.

## Consequences

- Chat cost: ~$0.0016/turn = ~$2.40/mo for ~50 turns/day.
- Quality holds across the persona tests we ran -- thinking mode is
  preserved through multi-turn meta-conversation.
- We pay OpenRouter latency (~1.5x of direct Groq for the first byte)
  but for a chat companion this is invisible.
- We're now tied to OpenRouter and DeepSeek's availability. Provider
  diversification (fallback to Groq Llama-70B on OpenRouter outage) is
  a future hardening project, not today's problem.

## Alternatives considered

- **Claude Sonnet 4.6**: best quality, ~30x more expensive. Reserved as
  the fallback when DeepSeek mis-steps on something important.
- **Stay on Groq**: cheapest, but the persona-steerability cap is real.
- **GPT-5.4**: similar price to Claude Sonnet, untested for our persona.
  Worth a comparison if DeepSeek regresses.
