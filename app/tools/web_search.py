"""web_search tool: Tavily / Parallel / SearXNG chosen automatically by env.

Backend priority: TAVILY_API_KEY > PARALLEL_API_KEY > SEARXNG_URL.
All keys come from the env; with none configured the tool self-disables
(with a status='disabled' result instead of an error).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.helpers.tool_helpers import maybe_truncate
from app.logger import logger
from app.tools.registry import ToolSpec

RESULT_LIMIT_DEFAULT = 5


async def _tavily(query: str, count: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": os.environ["TAVILY_API_KEY"], "query": query, "max_results": count},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])[:count]
    ]


async def _parallel(query: str, count: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.parallel.ai/v1/search",
            json={
                "objective": query,
                "search_queries": [query],
                "mode": os.getenv("PARALLEL_SEARCH_MODE", "fast"),
            },
            headers={
                "x-api-key": os.environ["PARALLEL_API_KEY"],
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return _parse_parallel_results(data, count)


def _parse_parallel_results(data: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """Map Parallel's {url, title, excerpts[]} to the common {title, url, snippet} shape."""
    parsed: list[dict[str, Any]] = []
    for r in data.get("results", [])[:count]:
        excerpts = [e for e in (r.get("excerpts") or []) if e]
        parsed.append(
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": excerpts[0] if excerpts else "",
            }
        )
    return parsed


async def _searxng(query: str, count: int) -> list[dict[str, Any]]:
    base = os.environ["SEARXNG_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{base}/search", params={"q": query, "format": "json"})
        resp.raise_for_status()
        data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])[:count]
    ]


def _pick_backend() -> tuple[str, Any] | None:
    if os.getenv("TAVILY_API_KEY"):
        return "tavily", _tavily
    if os.getenv("PARALLEL_API_KEY"):
        return "parallel", _parallel
    if os.getenv("SEARXNG_URL"):
        return "searxng", _searxng
    return None


async def run_web_search(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query", "")
    if not query.strip():
        return {"status": "error", "error": "query is required"}
    backend = _pick_backend()
    if backend is None:
        return {
            "status": "disabled",
            "error": (
                "web_search is disabled: set TAVILY_API_KEY, PARALLEL_API_KEY "
                "or SEARXNG_URL to enable it"
            ),
        }
    name, fn = backend
    count = int(arguments.get("count") or os.getenv("WEB_SEARCH_COUNT", RESULT_LIMIT_DEFAULT))
    try:
        results = await fn(query, count)
        return {
            "status": "success",
            "backend": name,
            "query": maybe_truncate(query, 2000),
            "results": results,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[web_search] backend {name}: {exc}")
        return {"status": "error", "error": f"{name}: {exc}"}


TOOL = ToolSpec(
    name="web_search",
    description=(
        "Web search via the configured backend (Tavily/Parallel/SearXNG). "
        "Returns titles, URLs and snippets. Disabled with a clear message "
        "when no API key is configured."
    ),
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Max results (default 5)"},
        },
    },
    run=run_web_search,
)
