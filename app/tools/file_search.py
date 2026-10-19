"""file_search tool: glob + regex grep across the sandbox allowed roots."""

from __future__ import annotations

import glob as globmod
import os
import re
from pathlib import Path
from typing import Any

from app.helpers.tool_helpers import maybe_truncate
from app.tools.registry import ToolSpec


def _roots() -> list[Path]:
    from app.pathsafety import allowed_roots

    return allowed_roots()


async def run_file_search(arguments: dict[str, Any]) -> dict[str, Any]:
    query_globs = arguments.get("glob")
    pattern = arguments.get("regex")
    search_roots = _roots()

    matches: list[str] = []
    if query_globs:
        for root in search_roots:
            for hit in globmod.glob(os.path.join(str(root), query_globs), recursive=True):
                matches.append(hit)
            if len(matches) >= 200:
                matches = sorted(set(matches))[:200]
                break

    grep_output: list[str] = []
    if pattern:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"status": "error", "error": f"invalid regex: {exc}"}
        targets: list[Path] = []
        for root in search_roots:
            targets.extend(Path(root).rglob("*"))
        count = 0
        for target in targets:
            if count >= int(arguments.get("max_matches") or 50):
                break
            if not target.is_file() or target.stat().st_size > 2_000_000:
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    count += 1
                    grep_output.append(f"{target}:{line_no}: {maybe_truncate(line.strip(), 300)}")
                    if count >= int(arguments.get("max_matches") or 50):
                        break

    if not query_globs and not pattern:
        return {"status": "error", "error": "provide 'glob' and/or 'regex'"}

    result: dict[str, Any] = {"status": "success", "roots": [str(r) for r in search_roots]}
    if query_globs:
        result["files"] = sorted(set(matches))
        result["file_count"] = len(result["files"])
    if pattern:
        result["matches"] = grep_output
        result["match_count"] = len(grep_output)
    return result


TOOL = ToolSpec(
    name="file_search",
    description=(
        "Find files by glob pattern and/or grep file contents by regex across "
        "the sandbox allowed roots. Returns matched file paths and/or "
        "file:line: match snippets."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "glob": {"type": "string", "description": "Glob pattern relative to sandbox root, e.g. '**/*.py'"},
            "regex": {"type": "string", "description": "Regex to search inside files"},
            "max_matches": {"type": "integer", "description": "Max grep matches (default 50)"},
        },
    },
    run=run_file_search,
)
