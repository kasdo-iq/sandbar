"""Google Gemini provider via the new `google-genai` SDK."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from google import genai
from google.genai import types as gtypes

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamEvent,
    ToolCall,
)


class GeminiProvider(LLMProvider):
    """Gemini chat via google.genai (Cloud/serverless, supports tools)."""

    api_family = "gemini"
    #: the SDK surfaces transient API errors as `genai.errors.APIError`
    retryable_errors: tuple[type[BaseException], ...] = ()

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        default_model: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        system_prompt: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self._default_model = default_model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if timeout:
            kwargs["http_options"] = gtypes.HttpOptions(timeout=f"{int(timeout * 1000)}ms")
        if extra_headers:
            # google-genai client does not take arbitrary headers per call;
            # documented limitation (kept in config for compat with others).
            kwargs["http_options"] = kwargs.get("http_options", gtypes.HttpOptions())
        self._client = genai.Client(**kwargs)
        if extra_headers:  # pragma: no cover - best effort
            pass

    @property
    def default_model(self) -> str:
        return self._default_model

    # ------------------------------------------------------------------
    # format conversion
    # ------------------------------------------------------------------

    def _convert(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """Map normalized messages to Gemini contents entries (dict form)."""
        contents: List[Dict[str, Any]] = []
        pending_calls: Dict[str, str] = {}
        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                parts = [{"text": msg.content} if msg.content else {"text": ""}]
                for tc in msg.tool_calls:
                    parts.append(
                        {
                            "function_call": gtypes.FunctionCall(
                                name=tc.name, args=tc.arguments
                            ).model_dump(exclude_none=True)
                        }
                    )
                    pending_calls[tc.id or tc.name] = tc.name
                contents.append({"role": "model", "parts": parts})
            elif msg.role == "tool":
                fn_name = msg.name or pending_calls.get(msg.tool_call_id or "", "tool")
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": gtypes.FunctionResponse(
                                    name=fn_name,
                                    response={"result": msg.content},
                                ).model_dump(exclude_none=True)
                            }
                        ],
                    }
                )
            elif msg.role in ("user", "assistant", "system"):
                role = "model" if msg.role == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": msg.content}]})
        return contents

    def _convert_tools(self, tools: List[Dict[str, Any]]) -> List[Any]:
        decls = []
        for tool in tools:
            fn = tool.get("function", tool)
            decls.append(
                gtypes.FunctionDeclaration.model_validate(
                    {
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters") or fn.get("input_schema")
                        or {"type": "object", "properties": {}},
                    }
                )
            )
        return [gtypes.Tool(function_declarations=decls)]

    def _parse(self, model: str, response: Any) -> tuple[str, List[ToolCall], Optional[Dict[str, Any]]]:
        text_parts: List[str] = []
        calls: List[ToolCall] = []
        usage = None
        candidate = None
        if response.candidates:
            candidate = response.candidates[0]
        if candidate and candidate.content:
            for part in candidate.content.parts or []:
                if part.text is not None:
                    text_parts.append(part.text)
                if part.function_call is not None:
                    calls.append(
                        ToolCall(
                            id=f"call_{len(calls)}_{part.function_call.name}",
                            name=part.function_call.name or "",
                            arguments=dict(part.function_call.args or {}),
                        )
                    )
        if response.usage_metadata:
            usage = response.usage_metadata.model_dump(exclude_none=True)
        return "".join(text_parts), calls, usage

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
        model_name = model or self._default_model
        response = await self._client.aio.models.generate_content(
            model=model_name,
            contents=self._convert(convo),  # type: ignore[arg-type]
            config=gtypes.GenerateContentConfig(
                system_instruction=system or None,
                tools=self._convert_tools(tools) if tools else None,
                temperature=temperature if temperature is not None else self._temperature,
                max_output_tokens=max_tokens if max_tokens is not None else self._max_tokens,
                **opts,
            ),
        )
        content, calls, usage = self._parse(model_name, response)
        finish = None
        if response.candidates:
            finish = response.candidates[0].finish_reason.name
        return LLMResponse(
            provider=self.name, model=model_name, content=content,
            tool_calls=calls, usage=usage, finish_reason=finish,
        )

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
        model_name = model or self._default_model
        final_text: List[str] = []
        async for chunk in await self._client.aio.models.generate_content_stream(
            model=model_name,
            contents=self._convert(convo),  # type: ignore[arg-type]
            config=gtypes.GenerateContentConfig(
                system_instruction=system or None,
                tools=self._convert_tools(tools) if tools else None,
                temperature=temperature if temperature is not None else self._temperature,
                max_output_tokens=max_tokens if max_tokens is not None else self._max_tokens,
                **opts,
            ),
        ):
            content, calls, usage = self._parse(model_name, chunk)
            if content:
                final_text.append(content)
                yield StreamEvent(type="delta", content=content)
            for tc in calls:
                yield StreamEvent(type="tool_call", tool_call=tc)
        yield StreamEvent(
            type="final",
            response=LLMResponse(
                provider=self.name,
                model=model_name,
                content="".join(final_text),
                tool_calls=[],
            ),
        )
