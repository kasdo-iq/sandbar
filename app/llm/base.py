"""Normalized LLM data models and the provider interface.

All providers convert their native formats into these models so callers
(and the Phase 4 agent loop) never see vendor-specific shapes.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A single normalized tool invocation requested by the model."""

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """A chat message in the normalized format.

    role="tool" messages carry tool results back to the model: set
    `tool_call_id` (OpenAI/Anthropic style) and `name` (the tool name,
    needed by Gemini's function-response format).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


class LLMResponse(BaseModel):
    """Non-streaming chat result in normalized form."""

    provider: str
    model: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None

    def model_dump_safe(self) -> dict[str, Any]:
        """Serializer used by endpoints (excludes nothing sensitive)."""
        return self.model_dump()


class StreamEvent(BaseModel):
    """One event of an SSE stream.

    type="delta": incremental `content`.
    type="tool_call": complete tool call (emitted once, args already parsed).
    type="final": full normalized LLMResponse (after all deltas).
    type="error": human-readable error string in `content`.
    """

    type: Literal["delta", "tool_call", "final", "error"]
    content: str = ""
    tool_call: ToolCall | None = None
    response: LLMResponse | None = None


def parse_tool_json(raw: str | bytes | None) -> dict[str, Any]:
    """Best-effort parse of a tool-arguments JSON payload, always dict."""
    if raw in (None, ""):
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)  # type: ignore[arg-type]
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class LLMProvider(ABC):
    """Abstract provider.

    Subclasses translate the normalized Message/tool formats to/from the
    vendor API. Transient (retry-worthy) failures must be raised as
    exceptions listed in `retryable_errors`.
    """

    #: exception classes for which the registry applies retry/backoff
    retryable_errors: tuple[type[BaseException], ...] = (Exception,)

    #: human-readable provider family, e.g. "openai-compatible"
    api_family: str = "unknown"

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> LLMResponse:
        """Send a chat completion request in normalized format."""

    @abstractmethod
    def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream deltas as normalized StreamEvents."""

    @property
    @abstractmethod
    def default_model(self) -> str:  # pragma: no cover - interface only
        """Model used when the caller does not override it."""

    @staticmethod
    def split_system_messages(messages: list[Message]) -> tuple[str, list[Message]]:
        """Extract system text and the remaining conversation, in order."""
        system_parts, rest = [], []
        for msg in messages:
            (system_parts if msg.role == "system" else rest).append(msg)
        return "\n\n".join(system_parts).strip(), rest
