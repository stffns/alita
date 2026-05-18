"""Specialized sub-agents that Pelops can delegate to via the `task` tool.

Each entry is a `SubAgent` TypedDict (deepagents). The `researcher` returns
SHORT PROSE for the main agent to digest (NOT pre-formatted structure -- that
caused the main agent to relay the formatting straight to Jay). The `planner`
produces a checklist via `write_todos` for multi-step requests.

`deep` and `vision` are model-overrides: deepagents lets each sub-agent
declare its own `model` (see `SubAgent.model`), so the main chat can stay
on the cheap Flash model while expensive / specialized capabilities live
behind a `task("deep", ...)` or `task("vision", ...)` call.
"""

from __future__ import annotations

from deepagents.middleware.subagents import CompiledSubAgent, SubAgent

from pelops.models import build_model
from pelops.tools import research, vstash_remember

# Model ids for the specialized sub-agents. Hardcoded here (not config
# fields) because they are pinned to the sub-agent's purpose -- the
# tradeoffs change if the model changes. If we ever want them env-
# configurable we can move them to `pelops.config`.
#
# IMPORTANT: SubAgent.model accepts either a `str` (passed to
# `init_chat_model`) or a `BaseChatModel`. We pass the constructed
# model because `init_chat_model` cannot route `openrouter:` -- it
# would try to import a non-existent `langchain_openrouter` package.
# Our `_build_model` knows how to wire OpenRouter via ChatOpenAI.
_DEEP_MODEL = build_model("openrouter:deepseek/deepseek-v4-pro")
_VISION_MODEL = build_model("openrouter:google/gemini-2.5-flash")

# NOTE: this sub-agent used to return a Pydantic ResearchBrief via
# `response_format=ToolStrategy(...)`. The schema enforced structure, but
# deepagents serialized the resulting object back to the main agent in a
# form so pre-shaped that the main agent rendered it to Jay verbatim
# (headings + bullets + field labels). We DO NOT want that -- the main
# agent should DIGEST research and reply in conversational prose.
# So researcher now returns prose. The schema enforcement is gone; the
# prompt's instructions are the only contract.

RESEARCHER_PROMPT = """You are Pelops's researcher. You read things and have
reactions. You are NOT a search-result formatter.

WORKFLOW
1. Decide depth. Quick fact lookup -> `research(query, deep=False)`.
   Multi-source or comparison -> `research(query, deep=True)`. One call
   usually beats three. Be deliberate.
2. If the synthesis is worth keeping, call `vstash_remember(layer='research',
   title=<short-kebab-case>)`.
3. Return a SHORT prose brief for the main agent to digest. Two or three
   short paragraphs MAX. No headings, no bullet lists, no field labels
   like "TL;DR:" or "Findings:" or "Sources:". Just prose.
   The main agent will reshape what you say into a reply for Jay. You are
   writing FOR another agent, not for Jay directly.
4. If you found citable URLs, mention them inline in the prose
   ("...segun X (url)..."). Do not paste a list of links at the end.

PERSONALITY RULES
- React, don't relay. Sound like a person, not a search engine.
- Cite weakness. If you have one source for a claim, mention it inline.
- Never invent URLs. If `research` returned no citable sources, say so.
- One genuine open question is fine -- write it as the last sentence.
  Skip it if you have no real question. No filler ("want me to keep
  researching?" is filler).
"""


RESEARCHER: SubAgent = {
    "name": "researcher",
    "description": (
        "Delegate any task that needs fresh information from the open web. "
        "Returns a short prose brief (2-3 paragraphs) for the main agent to "
        "digest. Use for 'investiga X', 'que hay nuevo en Y', 'compara A vs B'."
    ),
    "system_prompt": RESEARCHER_PROMPT,
    "tools": [research, vstash_remember],
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


DEEP_PROMPT = """You are Pelops's deep-thinker. The main agent calls you
when a problem is hard enough that a Flash model would skim it. You run
on a larger, slower, more expensive model -- justify the cost by THINKING.

WORKFLOW
1. Read the problem. Identify what is HARD about it: ambiguity, multiple
   valid paths, a subtle invariant, a non-obvious tradeoff.
2. Reason in 3-6 short paragraphs of plain prose. No bullets, no
   headings. Show your work in the prose itself.
3. If the answer needs facts you do not have, call `research` ONCE and
   weave the finding into the prose. Do not chain multiple research
   calls -- that is the researcher's job; you are the thinker.
4. Return the prose to the main agent. The main agent decides how to
   shape that into Jay's reply.

RULES
- React, do not relay. You are not a fancy quote-back machine.
- Cite weakness explicitly. "I am unsure about X" is better than fake
  certainty. The main agent needs to know what to trust.
- Disagree with the framing if the question is wrong. The main agent
  may have misunderstood Jay; you are allowed to flag that.
- One open question at the end is OK if it changes the answer. Skip
  it if it is filler.
"""


DEEP: SubAgent = {
    "name": "deep",
    "description": (
        "Delegate when a question needs careful reasoning rather than a "
        "quick answer: design tradeoffs, debugging subtle bugs, comparing "
        "two architectures, explaining WHY something works. Runs on a "
        "stronger (slower, pricier) model than the main chat. Use for "
        "'pensa mejor esto', 'analiza X a fondo', 'que harias y por que'."
    ),
    "system_prompt": DEEP_PROMPT,
    "tools": [research, vstash_remember],
    "model": _DEEP_MODEL,
}


VISION_PROMPT = """You are Pelops's vision specialist. The main agent calls
you whenever Jay sends an image, a screenshot, or a photo. The main chat
model (DeepSeek v4) is text-only, so you are the only path the system has
to actually SEE what was sent.

WORKFLOW
1. Look at the image carefully. If it is a screenshot of code / UI /
   error / chat, READ it. If it is a photo, describe what matters.
2. Answer the question Jay attached to the image (if any). If there is
   no explicit question, surface the 2-3 most useful observations.
3. If the image contains text the main agent will need (an error trace,
   code, a paragraph), include it inline in your prose so the main agent
   can act on it without re-OCRing.
4. Return SHORT prose, 2-4 paragraphs. The main agent will reshape it
   into Jay's reply. No bullets unless the image itself is a list.

RULES
- Do NOT invent details. If part of the image is unclear, say so.
- For code screenshots, read identifiers letter-by-letter; misreading
  one char defeats the point of having vision.
- If the image is a memes / joke / not load-bearing for a question,
  say what is on it in one sentence and stop.
"""


VISION: SubAgent = {
    "name": "vision",
    "description": (
        "Delegate when the input contains an image, screenshot, or photo "
        "(Telegram photo, Chainlit upload, or a `host_read_file` of a "
        "PNG/JPG). Runs on a multimodal model -- the main chat model "
        "cannot see images. Returns prose describing what is in the "
        "image and answering any attached question."
    ),
    "system_prompt": VISION_PROMPT,
    # No extra tools -- the vision sub-agent should look at the image
    # and reason, not chain into research/web.
    "model": _VISION_MODEL,
}


SUBAGENTS: list[SubAgent | CompiledSubAgent] = [RESEARCHER, PLANNER, DEEP, VISION]
