"""OpenAI-compatible chat provider.

One implementation reused for Groq and OpenRouter (and any other
OpenAI-compatible service) by wiring `base_url` from config.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
)

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamEvent,
    ToolCall,
    parse_tool_json,
)


class OpenAICompatibleProvider(LLMProvider):
    """Async OpenAI-compatible chat provider.

    Works for OpenAI, Groq (https://api.groq.com/openai/v1), OpenRouter
    (https://openrouter.ai/api/v1) and any similar service — all
    configuration differences are in `base_url`, `api_key` and
    `extra_headers`.
    """

    api_family = "openai-compatible"
    retryable_errors = (APIConnectionError, APITimeoutError)

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        default_model: str,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.name = name
        self._default_model = default_model
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            default_headers=dict(extra_headers) if extra_headers else None,
        )

    @property
    def default_model(self) -> str:
        return self._default_model

    # ------------------------------------------------------------------
    # format conversion
    # ------------------------------------------------------------------

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                out.append({"role": "system", "content": msg.content})
            elif msg.role == "user":
                out.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
                if msg.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": tc.id or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for i, tc in enumerate(msg.tool_calls)
                    ]
                out.append(item)
            elif msg.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "",
                        "content": msg.content,
                    }
                )
        return out

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools arrive already in OpenAI format: {type:name, function:{...}}.

        Accepts the common flat form {name, description, parameters} too.
        """
        converted = []
        for tool in tools:
            if "function" in tool:
                converted.append({"type": "function", "function": tool["function"]})
            else:
                converted.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.get("name", ""),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                        },
                    }
                )
        return converted

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------

    def _with_system_prefix(self, messages: list[Message]) -> list[Message]:
        if self._system_prompt and not any(m.role == "system" for m in messages):
            return [Message(role="system", content=self._system_prompt), *messages]
        return messages

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
        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": self._convert_messages(self._with_system_prefix(messages)),
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if temperature is not None or self._temperature is not None:
            kwargs["temperature"] = temperature if temperature is not None else self._temperature
        if max_tokens is not None or self._max_tokens is not None:
            kwargs["max_tokens"] = max_tokens if max_tokens is not None else self._max_tokens
        kwargs.update(opts)

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        tool_calls = [
            ToolCall(
                id=(tc.id or ""),
                name=tc.function.name,
                arguments=parse_tool_json(tc.function.arguments),
            )
            for tc in (choice.message.tool_calls or [])
        ]
        return LLMResponse(
            provider=self.name,
            model=kwargs["model"],
            content=choice.message.content or "",
            tool_calls=tool_calls,
            usage=getattr(resp, "usage", None) and getattr(resp.usage, "model_dump", lambda: None)(),
            finish_reason=choice.finish_reason,
        )

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {"stream": True, "stream_options": {"include_usage": True}}
        response_model = model or self._default_model
        pending_calls: dict[int, dict[str, Any]] = {}
        async for chunk in await self._client.chat.completions.create(
            model=response_model,
            messages=self._convert_messages(self._with_system_prefix(messages)),
            **({"tools": self._convert_tools(tools)} if tools else {}),
            temperature=temperature if temperature is not None else self._temperature,
            max_tokens=max_tokens if max_tokens is not None else self._max_tokens,
            **opts,
            **kwargs,
        ):
            if chunk.usage is not None:
                # usage arrives on the final chunk
                usage = chunk.usage.model_dump()
            else:
                usage = None
            for delta in chunk.choices and chunk.choices[0].delta and [chunk.choices[0].delta] or []:
                for idx, piece in enumerate(delta.tool_calls or []):
                    slot = pending_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if piece.id:
                        slot["id"] = piece.id
                    if piece.function and piece.function.name:
                        slot["name"] += piece.function.name
                    if piece.function and piece.function.arguments:
                        slot["args"] += piece.function.arguments
                text = delta.content
                if text:
                    yield StreamEvent(type="delta", content=text)
            finish = chunk.choices[0].finish_reason if chunk.choices else None
            if usage or finish:
                final = LLMResponse(
                    provider=self.name,
                    model=response_model,
                    content="",
                    tool_calls=[
                        ToolCall(id=s["id"], name=s["name"], arguments=parse_tool_json(s["args"] or "{}"))
                        for s in pending_calls.values()
                    ],
                    usage=usage,
                    finish_reason=finish,
                )
                for tc in final.tool_calls:
                    yield StreamEvent(type="tool_call", tool_call=tc)
                final.content = ""
                yield StreamEvent(type="final", response=final)
