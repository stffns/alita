---
name: briefing-builder
description: Assemble a tight morning briefing from the last 24h of ingested notes in vstash. Use when Jay asks for "el briefing", "que paso ayer", or when the scheduler triggers a briefing job.
---

# Briefing Builder

## Goal

Produce a 3-5 bullet brief covering the most signal-dense items Pelops has ingested
in the last 24 hours. Save the brief to vstash so Jay can recall it later.

## Workflow

1. **Pull recent material** -- call `vstash_recall` with queries derived from
   `PELOPS_TOPICS`. Use one query per topic, top_k=5. Discard items older than 24h
   if a timestamp is visible in the chunk.

2. **Cluster** -- group results by theme (a "theme" is 2+ items pointing at the
   same trend, repo, paper, or person). Drop singleton items unless they are
   genuinely surprising.

3. **Score signal**. For each theme, ask: *would Jay want to know this today?*
   Yes if it changes a decision, names a new tool he might use, or names a
   person he follows. No if it is recap of something he already knows.

4. **Write the brief** in this exact shape:

   ```
   # Briefing YYYY-MM-DD

   ## Top picks
   - [topic] one-line summary -- *why it matters: ...*
   - [topic] ...
   - [topic] ...

   ## Worth a glance
   - [topic] ...

   ## Skipped (and why)
   - briefly, one line per item you decided not to include
   ```

5. **Save it** with `vstash_remember(content=brief, title='briefing_<date>', tags='briefing')`.

6. **Return** the brief verbatim to the caller. Do not summarize the summary.

## Anti-patterns

- Do not include items without a clear "why it matters" -- if you cannot write one,
  the item is not signal.
- Do not pad. A 3-bullet brief with high signal beats a 7-bullet brief with noise.
- Do not invent items. If `vstash_recall` returns nothing, say "no fresh material
  in the last 24h" and stop.
