"""Endpoints for the tool registry and the agent loop.

GET  /tools            — tool list with JSON schemas (no secrets)
POST /tools/{name}/run — one tool invocation; body = arguments object
POST /agent/run        — SSE agent loop (goal + optional provider/tools)
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.logger import logger
from app.tools.registry import TOOL_REGISTRY

tools_router = APIRouter(tags=["tools"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])


@tools_router.get("/tools")
def list_tools() -> list[dict[str, Any]]:
    """Every registered tool with name/description/input_schema."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in TOOL_REGISTRY.list()
    ]


@tools_router.post("/tools/{name}/run")
async def run_tool(
    name: str, body: Annotated[dict[str, Any] | None, Body()] = None
) -> dict[str, Any]:
    """Execute one tool. Request body is the arguments object itself
    (or {"arguments": {...}}); responses mirror the tool result dicts."""
    payload = body if isinstance(body, dict) else {}
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else payload
    try:
        tool = TOOL_REGISTRY.get(name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"unknown tool '{name}'")
        return await asyncio.wait_for(tool.run(args), timeout=180)
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=504, detail=f"tool '{name}' timed out") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class AgentRunRequest(BaseModel):
    """POST /agent/run body."""

    goal: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    max_steps: int | None = None
    system_prompt: str | None = None
    total_timeout_seconds: float | None = None


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"data: {json.dumps({**payload, 'type': event}, ensure_ascii=False)}\n\n"


@agent_router.post("/run")
async def run_agent(req: AgentRunRequest):
    """The agent loop as an SSE event stream."""
    from app.agent.loop import run_agent_loop

    async def event_stream():
        try:
            async for event in run_agent_loop(
                goal=req.goal,
                provider=req.provider,
                model=req.model,
                tools=req.tools,
                max_steps=req.max_steps,
                system_prompt=req.system_prompt,
                total_timeout_seconds=req.total_timeout_seconds,
            ):
                event_type = event.pop("type", "event")
                yield _sse(event_type, event)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:  # client disconnect
            logger.info("[agent] client disconnected; loop cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"content": f"agent failed: {exc}"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
