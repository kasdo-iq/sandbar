"""http_download tool: fetch a URL to a file in the sandbox working dir."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.logger import logger
from app.tools.base import DEFAULT_WORKING_DIR
from app.tools.registry import ToolSpec
from app.tools.ssrf import MAX_REDIRECTS, SSRFBlocked, validate_url

TIMEOUT_DEFAULT = 60.0


def _default_target_name(url: str) -> str:
    name = Path(urlsplit(url).path).name or "download.bin"
    return str(Path(DEFAULT_WORKING_DIR, "downloads", name))


async def run_http_download(arguments: dict[str, Any]) -> dict[str, Any]:
    url = arguments.get("url", "")
    timeout = float(arguments.get("timeout") or os.getenv("HTTP_DOWNLOAD_TIMEOUT", TIMEOUT_DEFAULT))
    try:
        validate_url(url)
    except SSRFBlocked as exc:
        return {"status": "error", "error": f"blocked: {exc}"}

    destination = Path(arguments.get("dest") or _default_target_name(url))
    # containment: download target must resolve inside DEFAULT_WORKING_DIR
    from app import pathsafety

    try:
        destination = pathsafety.ensure_allowed(str(destination))
    except pathsafety.PathNotAllowed as exc:
        return {"status": "error", "error": str(exc)}

    destination.parent.mkdir(parents=True, exist_ok=True)
    redirects, current = 0, url
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        try:
            for _ in range(MAX_REDIRECTS + 1):
                resp = await client.get(current)
                if resp.is_redirect or resp.is_header_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        return {"status": "error", "error": "redirect without Location"}
                    try:
                        validate_url(urljoin(current, location))
                    except SSRFBlocked as exc:
                        return {"status": "error", "error": f"redirect blocked: {exc}"}
                    current = urljoin(current, location)
                    redirects += 1
                    continue
                with open(destination, "wb") as fh:
                    fh.write(resp.content)
                return {
                    "status": "success",
                    "url": str(resp.url),
                    "status_code": resp.status_code,
                    "path": str(destination),
                    "size_bytes": len(resp.content),
                    "redirects": redirects,
                }
            return {"status": "error", "error": f"too many redirects (>{MAX_REDIRECTS})"}
        except httpx.HTTPError as exc:
            logger.warning(f"[http_download] {url}: {exc}")
            return {"status": "error", "error": f"http error: {type(exc).__name__}"}


TOOL = ToolSpec(
    name="http_download",
    description=(
        "Download a public URL to a file inside the sandbox (default "
        "<workdir>/downloads/<filename>). SSRF-checked incl. redirect hops."
    ),
    input_schema={
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "http(s) URL to download"},
            "dest": {"type": "string", "description": "Target path inside the sandbox (optional)"},
            "timeout": {"type": "number", "description": "Seconds (default 60)"},
        },
    },
    run=run_http_download,
)
