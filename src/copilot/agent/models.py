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
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=name, temperature=0, max_tokens=2048, max_retries=3, timeout=60)


def triage_model() -> BaseChatModel:
    return chat_model(os.environ.get("COPILOT_TRIAGE_MODEL", DEFAULT_TRIAGE_MODEL))


def draft_model() -> BaseChatModel:
    return chat_model(os.environ.get("COPILOT_DRAFT_MODEL", DEFAULT_DRAFT_MODEL))


def require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Add it to .env or your shell, or use --model oracle to run offline."
        )
