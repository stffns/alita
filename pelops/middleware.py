"""Custom LangChain middleware for Pelops.

LoggingSummarization wraps the default deepagents SummarizationMiddleware
so every context-compression event is observable -- the summary is logged
and saved to vstash (layer='agent-action', label='context-compression')
so future Pelops can recall what was rolled away.

It ALSO replaces the default summary prompt. The deepagents default
explicitly tells the model to use `## SESSION INTENT / ## SUMMARY /
## ARTIFACTS / ## NEXT STEPS` headings. That format gets baked into
vstash as compression-event notes; later when the agent recalls them,
it copies the structure into chat replies. We want prose summaries
instead so nothing in vstash looks like a template the agent might
mimic when responding to the user.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import SummarizationMiddleware

log = logging.getLogger("pelops.middleware")


# Prose-only summary prompt. Deliberately gives NO example of the output
# shape -- examples in prompts get treated as templates by the model.
# The instructions are abstract enough that the model produces flowing
# sentences instead of a structured report.
PROSE_SUMMARY_PROMPT = """You are about to compress an old portion of your
conversation history so the model's context window does not overflow.
The result will REPLACE those old messages in the chat state.

Write the summary as plain prose -- one or two short paragraphs of
flowing sentences. Do NOT use headings of any kind (no `##`, no
ALL-CAPS labels). Do NOT use bullet lists. Do NOT use numbered lists.
Do NOT use bold field labels.

Cover, in flowing prose, what the user is trying to accomplish overall,
the most important decisions or rejected alternatives reached so far,
any files or artifacts touched (mention paths inline within sentences),
and what concrete work remains. Omit categories that have nothing to
report -- do not write "None" placeholders, just leave them out.

The summary is INPUT for the same model in future turns. It is NOT a
deliverable for the user. Be dense and information-rich; skip filler.

Respond with ONLY the prose summary. Nothing before or after.

<messages>
Messages to summarize:
{messages}
</messages>"""


class LoggingSummarization(SummarizationMiddleware):
    """Drop-in replacement for deepagents' default SummarizationMiddleware
    that surfaces compression events.

    Everything inherited works exactly as before. The single override is
    `_create_summary`, which calls super, then logs the event and saves
    the rendered summary to vstash so the agent can recall it later via
    `vstash_recall(query='context compression', layer='agent-action')`.
    """

    def _create_summary(self, messages_to_summarize):
        summary = super()._create_summary(messages_to_summarize)
        try:
            n = len(messages_to_summarize)
            preview = summary[:200] if isinstance(summary, str) else str(summary)[:200]
            log.warning(
                "CONTEXT COMPRESSED: rolled %d old messages into a summary (first 200 chars: %r)",
                n,
                preview,
            )
            # Save the compression event for future recall.
            from pelops.tools import record_agent_action

            record_agent_action(
                "context-compression",
                f"Context compression event: {n} old messages were rolled "
                f"into the following summary because the model's context "
                f"window was filling up.\n\n{summary}",
            )
        except Exception as exc:
            log.warning("compression logging failed: %s", exc)
        return summary
