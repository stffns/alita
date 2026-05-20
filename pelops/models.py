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

The `google_genai:` path exists because Gemini 3.x returns AIMessage
content as a list of content-block dicts (with thought-signature
extras) instead of a plain string. Most of Pelops's downstream code
assumes `.content` is a str (token-stream concat, history rebuild,
PDF export, etc). The `_FlattenContentChatModel` subclass below
normalizes every generation chunk on its way out so the rest of the
codebase is untouched.
"""

from __future__ import annotations

import os
from typing import Any

from langchain.chat_models import init_chat_model


def _flatten_content(content: Any) -> str:
    """Collapse a langchain content value into a plain string.

    Gemini 3.x returns content as `list[{type, text, extras}]` where
    `extras.signature` is a thought signature. We keep only the text
    blocks and drop everything else. Idempotent on str inputs.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _build_flatten_subclass():
    """Build a ChatGoogleGenerativeAI subclass that flattens list-content.

    Done lazily so importing this module does not pull in
    langchain-google-genai unless a `google_genai:` model is actually
    requested.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    class _FlattenContentChatModel(ChatGoogleGenerativeAI):
        def _generate(self, *args, **kwargs):
            result = super()._generate(*args, **kwargs)
            for gen in result.generations:
                gen.message.content = _flatten_content(gen.message.content)
            return result

        async def _agenerate(self, *args, **kwargs):
            result = await super()._agenerate(*args, **kwargs)
            for gen in result.generations:
                gen.message.content = _flatten_content(gen.message.content)
            return result

        def _stream(self, *args, **kwargs):
            for chunk in super()._stream(*args, **kwargs):
                chunk.message.content = _flatten_content(chunk.message.content)
                yield chunk

        async def _astream(self, *args, **kwargs):
            async for chunk in super()._astream(*args, **kwargs):
                chunk.message.content = _flatten_content(chunk.message.content)
                yield chunk

    return _FlattenContentChatModel


def build_model(model_id: str, temperature: float = 0.1):
    """Build a chat model from a `provider:model` string.

    Supported prefixes:

      ``openrouter:<model>``
          ChatOpenAI configured for ``openrouter.ai``. Requires
          ``OPENROUTER_API_KEY`` in the environment.
      ``google_genai:<model>``
          ChatGoogleGenerativeAI wrapped so list-content responses
          (Gemini 3.x thought-block format) are flattened to str.
          Requires ``GOOGLE_API_KEY`` in the environment.
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
    if model_id.startswith("google_genai:"):
        if not os.getenv("GOOGLE_API_KEY"):
            raise RuntimeError("model uses google_genai: prefix but GOOGLE_API_KEY is not set.")
        cls = _build_flatten_subclass()
        return cls(model=model_id.split(":", 1)[1], temperature=temperature)
    return init_chat_model(model=model_id, temperature=temperature)
