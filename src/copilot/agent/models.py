"""The LLMs the agent uses, through LangChain's Anthropic integration.

A small, fast model reads tickets; a stronger one writes replies. Both are
configurable with environment variables, so model choices can be compared in
the evals without code changes. Needs ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

DEFAULT_TRIAGE_MODEL = "claude-haiku-5-5"
DEFAULT_DRAFT_MODEL = "claude-sonnet-5-5"


def chat_model(name: str) -> BaseChatModel:
    """A Claude chat model whose structured outputs use Claude's native JSON schema output.

    Current Claude models reject forced tool use and non-default sampling settings, so the
    model is called with its defaults (no temperature) and returns structured output through
    `output_config.format` instead of a forced tool call. `max_tokens` covers any thinking as
    well as the answer, so it is set well above the size of a reply.
    """
    from langchain_anthropic import ChatAnthropic

    class StructuredClaude(ChatAnthropic):
        def with_structured_output(self, schema, *, method="json_schema", **kwargs):  # type: ignore[override]
            return super().with_structured_output(schema, method=method, **kwargs)

    return StructuredClaude(model=name, max_tokens=8192, max_retries=3, timeout=120)


def triage_model() -> BaseChatModel:
    return chat_model(os.environ.get("COPILOT_TRIAGE_MODEL", DEFAULT_TRIAGE_MODEL))


def draft_model() -> BaseChatModel:
    return chat_model(os.environ.get("COPILOT_DRAFT_MODEL", DEFAULT_DRAFT_MODEL))


def require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Add it to .env or your shell, or use --model oracle to run offline."
        )
