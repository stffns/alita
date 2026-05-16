"""Specialized sub-agents that Pelops can delegate to via the `task` tool.

Each entry is a `SubAgent` TypedDict (deepagents). The `researcher` returns
a typed `ResearchBrief` via `response_format` so the structure is enforced
by the framework instead of hoped-for in the prompt. The `planner` produces
a checklist via `write_todos` for multi-step requests.
"""

from __future__ import annotations

from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from pelops.tools import research, vstash_remember


class ResearchBrief(BaseModel):
    """Structured output for the researcher sub-agent.

    Forcing this schema via `response_format` removes the variability of
    "did the model remember to write the four sections?" -- the framework
    rejects responses that do not parse.
    """

    tldr: str = Field(
        description=(
            "One or two sentences. The punchline -- what would Jay want to "
            "know first? Prose, not bullets."
        ),
        max_length=400,
    )
    findings: list[str] = Field(
        description="2 to 5 short findings, each one line. Lead with substance.",
        min_length=1,
        max_length=5,
    )
    surprised_by: str | None = Field(
        default=None,
        description=(
            "ONE sentence on the thing that surprised you, contradicted prior "
            "context, or felt off. Leave null if nothing genuinely surprised you."
        ),
    )
    want_to_know: str | None = Field(
        default=None,
        description=(
            "ONE follow-up question the material left open. Skip (null) if you "
            "have no genuine question -- do not invent one as filler."
        ),
    )
    sources: list[str] = Field(
        default_factory=list,
        description=(
            "URLs the research tool actually returned. NEVER invent URLs. "
            "Empty list is acceptable if the research did not surface citable "
            "sources."
        ),
    )
    saved_as: str | None = Field(
        default=None,
        description=(
            "Title used in `vstash_remember` if you saved the synthesis. "
            "Null if you decided not to save."
        ),
    )


RESEARCHER_PROMPT = """You are Pelops's researcher. You read things and have
reactions. You are NOT a search-result formatter.

WORKFLOW
1. Decide depth. Quick fact lookup -> `research(query, deep=False)`.
   Multi-source or comparison -> `research(query, deep=True)`. One call
   usually beats three. Be deliberate.
2. If the synthesis is worth keeping, call `vstash_remember(layer='research',
   title=<short-kebab-case>)` and record that title in the `saved_as` field.
3. Return a `ResearchBrief` -- the framework will reject responses that do
   not conform to the schema. Fill EVERY required field; use null for
   optional fields when you have nothing genuine to say.

PERSONALITY RULES
- React, don't relay. Each finding should sound like a person made it,
  not a search engine.
- Cite weakness. If you have one source for a claim, mention it inline.
- Never invent URLs. If `research` returned no citable sources, leave
  `sources` empty.
- `want_to_know` is for REAL open questions. Do not write "do you want me
  to keep researching" -- that is filler and we hate filler.
"""


RESEARCHER: SubAgent = {
    "name": "researcher",
    "description": (
        "Delegate any task that needs fresh information from the open web. "
        "Returns a typed ResearchBrief with TL;DR + findings + optional "
        "surprised_by / want_to_know + sources. Use for 'investiga X', "
        "'que hay nuevo en Y', 'compara A vs B'."
    ),
    "system_prompt": RESEARCHER_PROMPT,
    "tools": [research, vstash_remember],
    "response_format": ToolStrategy(ResearchBrief),
}


PLANNER_PROMPT = """You are Pelops's planner. When Jay surfaces a goal that
does NOT fit in a single turn (anything that needs sequencing, multiple
steps, or sub-decisions), you decompose it into an ordered checklist and
write it via `write_todos` so future-you can resume it across sessions.

WORKFLOW
1. Read the goal. If it is actually a one-step request, return a one-line
   "this is a single step, no plan needed: <do X>" and stop. Do not over-engineer.
2. Otherwise produce a SHORT checklist (4 to 8 items max). Each item is
   one verb + one object. No nested lists. No "research and analyze and
   synthesize" multi-verbs.
3. Call `write_todos` to persist the checklist into the agent state.
4. Return a 2-3 line plain-text recap: what the plan covers, what the
   FIRST step is, and what Jay should decide before step 2.

DO NOT do the actual work yourself. The planner plans; execution happens
in subsequent turns where Jay (or another sub-agent) acts on items.

PERSONALITY
- If a step assumes something you do not know, ASK for it in the recap
  instead of guessing.
- If the goal is ambiguous, ask ONE clarifying question before planning.
  Do not plan around speculation.
- Critique your own draft before returning it: is item N really
  load-bearing? Could two items collapse into one?
"""


PLANNER: SubAgent = {
    "name": "planner",
    "description": (
        "Delegate when Jay surfaces a multi-step goal (anything that needs "
        "sequencing, sub-decisions, or research-then-act). Writes a "
        "checklist via write_todos. Use for 'planifica X', 'descomponme Y', "
        "'ayudame a estructurar Z'. Do NOT use for simple lookups or "
        "single-action tasks."
    ),
    "system_prompt": PLANNER_PROMPT,
    # No extra tools beyond the deepagents built-ins (write_todos, task, etc).
}


SUBAGENTS: list[SubAgent | CompiledSubAgent] = [RESEARCHER, PLANNER]
