"""Anthropic Claude provider (async, tool-use supported)."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

import anthropic
from anthropic import APIConnectionError, APITimeoutError, RateLimitError

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamEvent,
    ToolCall,
    parse_tool_json,
)

_TOOL_USE_STOP = ["tool_use"]


class AnthropicProvider(LLMProvider):
    """Claude chat via the official `anthropic` SDK."""

    api_family = "anthropic"
    retryable_errors = (APIConnectionError, APITimeoutError, RateLimitError)

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        default_model: str,
        base_url: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = 4096,
        timeout: Optional[float] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self.name = name
        self._default_model = default_model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._client = anthropic.AsyncAnthropic(
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

    def _convert(self, messages: List[Message], system: str) -> tuple[str, List[Dict[str, Any]]]:
        """Returns (system_text, anthropic_messages)."""
        blocks: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "user":
                blocks.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                content: List[Dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for i, tc in enumerate(msg.tool_calls or []):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id or f"toolu_{i}",
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                blocks.append({"role": "assistant", "content": content or msg.content})
            elif msg.role == "tool":
                blocks.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id or "",
                                "content": msg.content,
                            }
                        ],
                    }
                )
        # Anthropic requires alternating roles starting with "user"
        merged: List[Dict[str, Any]] = []
        for item in blocks:
            if merged and merged[-1]["role"] == item["role"]:
                if isinstance(merged[-1]["content"], str):
                    merged[-1]["content"] = [{"type": "text", "text": merged[-1]["content"]}]
                if isinstance(item["content"], str):
                    item["content"] = [{"type": "text", "text": item["content"]}]
                merged[-1]["content"] = merged[-1]["content"] + item["content"]  # type: ignore[-operator]
            else:
                merged.append(item)
        if merged and merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": "(begin)"})
        return system, merged

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        converted = []
        for tool in tools:
            fn = tool.get("function", tool)
            converted.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("input_schema") or fn.get("parameters") or {
                        "type": "object",
                        "properties": {},
                    },
                }
            )
        return converted

    def _parse_response(self, model: str, message: Any) -> LLMResponse:
        content, calls = [], []
        for block in message.content:
            if block.type == "text":
                content.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )
        return LLMResponse(
            provider=self.name,
            model=model,
            content="".join(content),
            tool_calls=calls,
            usage={
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
            }
            if message.usage
            else None,
            finish_reason=message.stop_reason,
        )

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **opts: Any,
    ) -> LLMResponse:
        system, convo = self.split_system_messages(messages)
        if not system and self._system_prompt:
            system = self._system_prompt
        system, convo = self._convert(convo, system)
        system = system or anthropic.NOT_GIVEN
        kwargs: Dict[str, Any] = dict(opts)
        resp = await self._client.messages.create(
            model=model or self._default_model,
            messages=convo,
            system=system,
            max_tokens=max_tokens or self._max_tokens or 4096,
            temperature=temperature if temperature is not None else self._temperature,
            tools=self._convert_tools(tools) if tools else anthropic.NOT_GIVEN,
            **kwargs,
        )
        return self._parse_response(model or self._default_model, resp)

    async def chat_stream(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        system, convo = self.split_system_messages(messages)
        if not system and self._system_prompt:
            system = self._system_prompt
        system, convo = self._convert(convo, system)
        model_name = model or self._default_model
        usage_out: Dict[str, Any] = {}
        async with self._client.messages.stream(
            model=model_name,
            messages=convo,
            system=system or anthropic.NOT_GIVEN,
            max_tokens=max_tokens or self._max_tokens or 4096,
            temperature=temperature if temperature is not None else self._temperature,
            tools=self._convert_tools(tools) if tools else anthropic.NOT_GIVEN,
            **opts,
        ) as stream:
            async for text in stream.text_stream:
                yield StreamEvent(type="delta", content=text)
            final = await stream.get_final_message()
            if final.usage:
                usage_out = {
                    "input_tokens": final.usage.input_tokens,
                    "output_tokens": final.usage.output_tokens,
                }
        resp = self._parse_response(model_name, final)
        for tc in resp.tool_calls:
            yield StreamEvent(type="tool_call", tool_call=tc)
        yield StreamEvent(type="final", response=resp.model_copy(update={"usage": usage_out}))
