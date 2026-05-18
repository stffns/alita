"""Chat-model construction helpers shared by `pelops.agent` and `pelops.subagents`.

Originally lived in `pelops.agent.py` as `_build_model`. Moved here when
`pelops.subagents` needed it for SubAgent.model overrides -- importing
`_build_model` from `pelops.agent` while `pelops.agent` was importing
SUBAGENTS would deadlock the module load.

The OpenRouter path is the load-bearing reason this exists. LangChain's
`init_chat_model` does not understand `openrouter:` prefixes -- it tries
to import a non-existent `langchain_openrouter` package and crashes.
Going through `ChatOpenAI` against the OpenRouter base URL is the
documented integration.
"""

from __future__ import annotations

import os

from langchain.chat_models import init_chat_model


def build_model(model_id: str, temperature: float = 0.1):
    """Build a chat model from a `provider:model` string.

    Supported prefixes:

      ``openrouter:<model>``
          ChatOpenAI configured for ``openrouter.ai``. Requires
          ``OPENROUTER_API_KEY`` in the environment.
      ``groq:<model>``
          Routed by ``init_chat_model``.
      anything else
          Falls through to ``init_chat_model`` (provider auto-detect).
    """
    if model_id.startswith("openrouter:"):
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("model uses openrouter: prefix but OPENROUTER_API_KEY is not set.")
        return ChatOpenAI(
            model=model_id.split(":", 1)[1],
            api_key=SecretStr(api_key),
            base_url="https://openrouter.ai/api/v1",
            temperature=temperature,
        )
    return init_chat_model(model=model_id, temperature=temperature)
