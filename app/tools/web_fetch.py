"""web_fetch tool: HTTP GET with size/time limits and SSRF checks."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin

import httpx

from app.helpers.tool_helpers import maybe_truncate
from app.logger import logger
from app.tools.registry import ToolSpec
from app.tools.ssrf import MAX_REDIRECTS, SSRFBlocked, validate_url

MAX_BYTES_DEFAULT = 5 * 1024 * 1024
TIMEOUT_DEFAULT = 30.0


async def run_web_fetch(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "")
    max_bytes = int(arguments.get("max_bytes") or os.getenv("WEB_FETCH_MAX_BYTES", MAX_BYTES_DEFAULT))
    timeout = float(arguments.get("timeout") or os.getenv("WEB_FETCH_TIMEOUT", TIMEOUT_DEFAULT))

    try:
        validate_url(url)
    except SSRFBlocked as exc:
        return {"status": "error", "error": f"blocked: {exc}"}

    redirects, current = 0, url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, headers={"User-Agent": "manus-open-sandbox/1.0"}
    ) as client:
        try:
            for _ in range(MAX_REDIRECTS + 1):
                resp = await client.get(current)
                if resp.is_redirect or resp.is_header_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        return {"status": "error", "error": "redirect without Location"}
                    # re-validate every hop BEFORE following (AUDIT SSRF rule)
                    try:
                        validate_url(urljoin(current, location))
                    except SSRFBlocked as exc:
                        return {
                            "status": "error",
                            "error": f"redirect blocked: {exc}",
                            "redirect_url": location,
                        }
                    current = urljoin(current, location)
                    redirects += 1
                    continue
                content = resp.content[:max_bytes]
                truncated = len(resp.content) > max_bytes
                text = content.decode(resp.encoding or "utf-8", errors="replace")
                return {
                    "status": "success",
                    "url": str(resp.url),
                    "status_code": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "size_bytes": len(resp.content),
                    "truncated": truncated or len(text) > 16000,
                    "redirects": redirects,
                    "content": maybe_truncate(text, 16000),
                }
            return {"status": "error", "error": f"too many redirects (>{MAX_REDIRECTS})"}
        except httpx.HTTPError as exc:
            logger.warning(f"[web_fetch] {url}: {exc}")
            return {"status": "error", "error": f"http error: {type(exc).__name__}: {exc}"}


TOOL = ToolSpec(
    name="web_fetch",
    description=(
        "HTTP GET a public URL inside the sandbox and return its body as text. "
        "Private/loopback/metadata IP ranges and cross-hop redirects to them "
        "are blocked. Size and time limited."
    ),
    input_schema={
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "http(s) URL to fetch"},
            "max_bytes": {"type": "integer", "description": "Max body bytes kept (default 5MB)"},
            "timeout": {"type": "number", "description": "Request timeout seconds (default 30)"},
        },
    },
    run=run_web_fetch,
)
