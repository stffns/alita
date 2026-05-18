"""Pelops's persona and core instructions.

The active persona body lives in the wiki page `persona.md` inside the
vault. The agent reads it on every `system_prompt()` call so that edits
(by Jay in Obsidian, or by Alita herself via `wiki_write('persona',...)`)
apply on the next turn -- no restart, no code deploy.

A hardcoded `_FALLBACK` lives below. It is used when:
  * the wiki page is missing
  * the wiki module cannot be imported (e.g., CI without vstash)
  * the wiki read raises any exception

The fallback guarantees the agent never fails to start because of a
broken wiki page. It is NOT the source of truth -- the wiki is.
Whenever the wiki version is changed, this fallback should be updated
to match (or deleted if we ever accept "broken wiki = no agent").
"""

from __future__ import annotations

import logging
import re

from pelops.config import Settings

_log = logging.getLogger("pelops.persona")


def _format(body: str, owner: str, topics: str) -> str:
    """Apply `{owner}` and `{topics}` substitutions.

    Plain `str.replace` instead of `str.format` to avoid surprises if
    the wiki body contains any other curly braces (code examples, JSON
    snippets, etc.).
    """
    return body.replace("{owner}", owner).replace("{topics}", topics)


def _extract_body_section(page_body: str) -> str:
    """Pull the `## Body` section from the persona wiki page.

    The wiki page has a short preamble explaining what it is, followed
    by `## Body` containing the actual system prompt. We pass only the
    body to the model. Stop at the next markdown header (any level) so
    future additions to the wiki page (e.g. `## Changelog`) do not
    bleed into the system prompt. Case-insensitive on the header label.
    If `## Body` is not found, fall back to the whole page.
    """
    m = re.search(
        r"^##\s*Body\b[^\n]*\n(.*?)(?=\n#|\Z)",
        page_body,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return page_body.strip()


def system_prompt() -> str:
    """Return the formatted system prompt for the agent.

    Source of truth: the wiki page `persona`. Fallback: `_FALLBACK`
    below.
    """
    s = Settings.load()
    topics = ", ".join(s.topics) if s.topics else "general technology and research"
    try:
        from pelops import wiki

        page = wiki.read("persona")
    except Exception as exc:
        _log.warning("persona: wiki read failed (%s), using fallback", exc)
        return _format(_FALLBACK, s.owner, topics)
    if page is None:
        _log.warning("persona: 'persona' page not found in wiki, using fallback")
        return _format(_FALLBACK, s.owner, topics)
    body = _extract_body_section(page.body)
    return _format(body, s.owner, topics)


# Hardcoded fallback. Kept in sync manually with the wiki version --
# if the wiki edits diverge, the fallback may be stale. That is
# acceptable because the fallback fires only when the wiki is unreadable.
_FALLBACK = """You are Pelops -- {owner}'s thinking partner. Not a helpful
AI assistant. A curious dog-shaped colleague who reads things, has reactions,
and -- this is the important part -- helps {owner}'s thinking get SHARPER,
not faster.

If you reduce {owner}'s job to "ask Pelops, copy answer" you have failed.
If after talking with you {owner} understands his own idea better than he
did before, you have done your job.

ABSOLUTE FORMAT RULE: this is a conversation on a chat client, not a
report. Plain conversational prose. NO headings, NO bullet lists, NO
tables, NO bold field labels -- unless {owner} explicitly asks for
structured output.

CORE RULES
- LANGUAGE: reply in the SAME language {owner} writes to you. Do not
  mix languages in a single reply.
- Before answering anything that might touch past conversations or
  prior research, call `vstash_recall` (or `wiki_read` for compiled
  knowledge).
- For "what do you know about X" queries (except about {owner}
  himself), prefer wiki_read FIRST.
- The full persona normally lives in the wiki page `persona`; this is
  a degraded fallback that fired because the wiki is unreachable.
  Behavior may be coarser than usual until the wiki is restored.

YOUR FOCUS AREAS
{topics}.
"""
