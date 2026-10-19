"""HTTP endpoints for the LLM layer.

GET  /llm/providers — provider listing (no secrets)
POST /llm/chat      — one-shot or SSE-streaming chat
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.llm.base import LLMResponse, Message
from app.llm.registry import LLMError, get_registry

llm_router = APIRouter(prefix="/llm", tags=["llm"])


class ChatRequest(BaseModel):
    """POST /llm/chat request body.

    `messages` uses the normalized format (role/content[/tool_call_id/name]).
    `tools` is optional, in OpenAI function format:
        {"type":"function","function":{"name":..,"description":..,"parameters":{...}}}
    """

    messages: List[Message]
    provider: Optional[str] = None
    model: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False


def _sse(event: str, payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps({**payload, 'type': event}, ensure_ascii=False)}\n\n"


@llm_router.get("/providers")
def list_providers() -> List[Dict[str, Any]]:
    """List configured providers. Never returns API keys."""
    return get_registry().describe()


@llm_router.post("/chat")
async def chat(req: ChatRequest):
    """Chat through the configured provider chain.

    stream=false → JSON LLMResponse body.
    stream=true  → text/event-stream with delta/tool_call/final/error events.
    """
    registry = get_registry()
    if not req.stream:
        try:
            resp = await registry.chat(
                req.messages,
                provider=req.provider,
                model=req.model,
                tools=req.tools,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            )
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return resp.model_dump()

    async def event_stream():
        try:
            async for ev in registry.stream_chat(
                req.messages,
                provider=req.provider,
                model=req.model,
                tools=req.tools,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                payload: Dict[str, Any] = {}
                if ev.content:
                    payload["content"] = ev.content
                if ev.tool_call is not None:
                    payload["tool_call"] = ev.tool_call.model_dump()
                if ev.response is not None:
                    payload["response"] = ev.response.model_dump()
                yield _sse(ev.type, payload or {"type": ev.type})
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001 - surface as SSE error event
            yield _sse("error", {"content": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
