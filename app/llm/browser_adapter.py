"""LangChain model adapter for the vendored browser_use Agent.

browser_use calls `llm.with_structured_output(AgentOutput, include_raw=True)`
and needs vision-capable message arrays, so the adapter returns a LangChain
ChatModel pointing at the configured provider:

- openai-compatible providers (incl. Groq/OpenRouter/custom base_url) →
  ChatOpenAI with the provider's base_url.
- anthropic → ChatAnthropic (this is also the pre-existing default behavior).
- gemini → ChatOpenAI against Google's OpenAI-compatible endpoint.

Behavior without any configured providers is unchanged: callers may keep
passing their own Claude/ChatAnthropic instance to `Agent`.
"""

from __future__ import annotations

import os
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.llm.registry import LLMError, get_registry

_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _openai_chat_model(
    api_key: str, base_url: Optional[str], model: str, temperature: Optional[float] = None
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url,
        temperature=temperature,
    )


def get_browser_use_llm(
    provider_name: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> object:
    """Build a LangChain chat model from the configured provider chain.

    Raises LLMError when nothing is configured. Args:
      provider_name: override (else registry default_provider).
      model: override (else provider default model).
      temperature: caller-controlled (vendored Agent defaults its own).
    """
    registry = get_registry()
    name = provider_name or registry.config.default_provider
    if not name:
        raise LLMError(
            "no default provider — configure providers.yaml/LLM_DEFAULT_PROVIDER "
            "or pass an explicit provider_name"
        )
    pc = next((p for p in registry.config.providers if p.name == name), None)
    if pc is None:
        raise LLMError(f"provider '{name}' is not configured")
    if not os.environ.get(pc.api_key_env):
        raise LLMError(f"provider '{name}' needs env var {pc.api_key_env}")
    if pc.api_family == "anthropic":
        return ChatAnthropic(
            model=model or pc.model,
            api_key=SecretStr(os.environ[pc.api_key_env]),
            temperature=temperature,
            max_tokens=pc.max_tokens or 4096,
            timeout=pc.timeout,
        )
    if pc.api_family == "gemini":
        return _openai_chat_model(
            api_key=os.environ[pc.api_key_env],
            base_url=_GEMINI_OPENAI_BASE,
            model=model or pc.model,
            temperature=temperature,
        )
    # openai-compatible (groq, openrouter, custom)
    return _openai_chat_model(
        api_key=os.environ[pc.api_key_env],
        base_url=pc.base_url,
        model=model or pc.model,
        temperature=temperature,
    )


__all__ = ["get_browser_use_llm"]
