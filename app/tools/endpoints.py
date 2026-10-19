"""Endpoints for the tool registry (GET /tools, POST /tools/{name}/run)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from app.tools.registry import TOOL_REGISTRY

tools_router = APIRouter(tags=["tools"])


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
