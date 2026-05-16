"""Custom LangChain middleware for Pelops.

LoggingSummarization wraps the default deepagents SummarizationMiddleware
so every context-compression event is observable -- the summary is logged
and saved to vstash (layer='agent-action', label='context-compression')
so future Pelops can recall what was rolled away.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import SummarizationMiddleware

log = logging.getLogger("pelops.middleware")


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
