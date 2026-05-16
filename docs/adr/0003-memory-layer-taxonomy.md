# 3. vstash memory layer taxonomy

- Status: Accepted
- Date: 2026-05-15
- Deciders: Jay

## Context

We started with one big `vstash` collection and a free-form `tags` field.
First quality test surfaced the problem: when the agent recalled "what
do you know about Jay", it mixed personal facts with briefing items
from RSS feeds, treating both as if they were things Jay had told it.
The result hallucinated Jay's "current project" from items in a morning
briefing.

The root cause: the recall mechanism had no way to filter by intent.
Every note looked the same.

## Decision

We introduced an enumerated `layer` field on every vstash note. The
canonical layers are:

  user-fact      things Jay told Pelops about himself
  research       syntheses Pelops produced about external topics
  briefing       morning briefings produced by the scheduler
  consolidated   semantic distillations of recent briefings
  rss            raw RSS ingest dumps
  agent-action   things Pelops has pushed to Jay (its own history)
  thoughts       half-formed ideas Jay raised in conversation
  episodic       raw chat turns (user msg + agent response)
  session-state  rolling "where we are right now" snapshots

The persona prompt explicitly forbids mixing layers in recall ("if Jay
asks 'what do you know about me', recall layer='user-fact' ONLY").
`vstash_remember` enforces a layer at write time.

## Consequences

- Quality of "about me" queries went from confabulation to honest
  "I do not have notes on that yet".
- The agent has a structural reason to *think* about which layer
  something belongs to before saving. That alone improves consistency.
- The taxonomy will probably need to grow (e.g. a `goal` layer). Adding
  layers is cheap; deprecating them is the only painful direction.

## Alternatives considered

- **Free-form tags only**: the original. Recall filtering by tag was
  possible but never used because the agent could not be trusted to
  pick tags consistently across saves.
- **Multiple vstash collections**: physically separate stores. Avoided
  to keep one source of truth and one embedding model.
- **Provenance metadata only** (source URL etc): too granular for the
  thing we wanted, which is "is this about Jay, the world, or my own
  history".
