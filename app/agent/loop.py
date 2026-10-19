"""LLM tool-calling agent loop.

Drives chat completions with the Phase 2 LLM registry against sandbox
tools (Phase 4 registry). Steps stream as plain dicts that the endpoint
wraps into SSE frames:

  step_start -> assistant_text -> tool_call -> tool_result -> ...
  final_answer | error
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from app.helpers.tool_helpers import maybe_truncate
from app.llm.base import LLMResponse, Message, ToolCall
from app.llm.registry import LLMError, get_registry
from app.logger import logger
from app.tools.registry import TOOL_REGISTRY

DEFAULT_MAX_STEPS = 15
DEFAULT_TOTAL_TIMEOUT = 300.0
TOOL_CALL_TIMEOUT = 120.0

DEFAULT_SYSTEM_PROMPT = (
    "You are an autonomous agent inside the Manus sandbox. You have tools "
    "available. Solve the user's goal step by step: call tools when you need "
    "information or side effects, then answer. Prefer the fewest tool calls "
    "necessary. When you are done, reply with the final answer and do not "
    "call more tools."
)


def _serialize_tool_result(result: Any) -> str:
    """Compact JSON view of a tool result for the next model turn."""
    if isinstance(result, dict):
        text: Any = result.get("result", result)
        if isinstance(text, (str, int, float, bool)) or text is None:
            content = str(text)
        else:
            content = json.dumps(result, default=str, ensure_ascii=False)
    else:
        content = str(result)
    return maybe_truncate(content, int(os.getenv("AGENT_OUTPUT_MAX_CHARS", "16000")))


async def run_agent_loop(
    *,
    goal: str,
    provider: str | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    max_steps: int | None = None,
    system_prompt: str | None = None,
    total_timeout_seconds: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield step events; terminates with final_answer or error."""
    max_steps = int(max_steps or os.getenv("AGENT_MAX_STEPS", DEFAULT_MAX_STEPS))
    total_timeout = float(total_timeout_seconds or os.getenv("AGENT_TOTAL_TIMEOUT", DEFAULT_TOTAL_TIMEOUT))
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")

    llm = get_registry()
    fns = TOOL_REGISTRY.openai_functions(tools)
    if not TOOL_REGISTRY.list():
        yield {"type": "error", "content": "no tools registered (unexpected startup failure)"}
        return

    messages: list[Message] = [
        Message(role="system", content=system_prompt or os.getenv("AGENT_SYSTEM_PROMPT", "") or DEFAULT_SYSTEM_PROMPT),
        Message(role="user", content=goal),
    ]
    started = time.monotonic()
    logger.info(f"[agent] goal={goal[:120]!r} provider={provider or llm.config.default_provider} tools={tools}")

    try:
        async with asyncio.timeout(total_timeout):
            for step in range(max_steps):
                remaining = total_timeout - (time.monotonic() - started)
                yield {"type": "step_start", "step": step, "remaining_seconds": round(max(remaining, 0.0), 1)}
                try:
                    response: LLMResponse = await llm.chat(
                        messages, provider=provider, model=model, tools=fns or None
                    )
                except LLMError as exc:
                    yield {"type": "error", "content": f"LLM failure at step {step}: {exc}"}
                    return

                if response.content:
                    yield {"type": "assistant_text", "step": step, "content": maybe_truncate(response.content, 8000)}

                tool_calls: list[ToolCall] = response.tool_calls
                if not tool_calls:
                    yield {
                        "type": "final_answer",
                        "steps": step + 1,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                        "content": response.content,
                        "usage": response.usage,
                    }
                    return

                messages.append(
                    Message(role="assistant", content=response.content, tool_calls=tool_calls)
                )
                for tc in tool_calls:
                    yield {
                        "type": "tool_call",
                        "step": step,
                        "tool_call": {"id": tc.id or tc.name, "name": tc.name, "arguments": tc.arguments},
                    }
                    try:
                        tool = TOOL_REGISTRY.require(tc.name)
                        result: Any = await asyncio.wait_for(tool.run(tc.arguments), timeout=TOOL_CALL_TIMEOUT)
                    except KeyError:
                        result = {"status": "error", "error": f"unknown tool '{tc.name}'"}
                    except TimeoutError:
                        result = {"status": "error", "error": f"tool '{tc.name}' timed out after {TOOL_CALL_TIMEOUT}s"}
                    except Exception as exc:  # noqa: BLE001 - tool errors feed back to the model
                        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

                    yield {
                        "type": "tool_result",
                        "step": step,
                        "tool_call_id": tc.id or tc.name,
                        "name": tc.name,
                        "result": result,
                    }
                    messages.append(
                        Message(
                            role="tool",
                            content=_serialize_tool_result(result),
                            tool_call_id=tc.id or tc.name,
                            name=tc.name,
                        )
                    )
            yield {
                "type": "error",
                "content": f"max_steps ({max_steps}) reached without a final answer",
                "steps": max_steps,
            }
    except TimeoutError:
        yield {
            "type": "error",
            "content": f"total timeout ({total_timeout}s) reached without a final answer",
        }
