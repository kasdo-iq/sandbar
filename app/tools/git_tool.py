"""git tool: status/diff/commit/log via the CLI. No push by default."""

from __future__ import annotations

import shlex
from typing import Any

from app.helpers.tool_helpers import maybe_truncate, run_shell
from app.tools.base import DEFAULT_WORKING_DIR
from app.tools.registry import ToolSpec

ALLOWED = ["status", "diff", "commit", "log"]


def _git_available() -> bool:
    """git is optional in the container image (Node PPA install only)."""
    import shutil as _shutil

    return _shutil.which("git") is not None


async def run_git(arguments: dict[str, Any]) -> dict[str, Any]:
    if not _git_available():
        # attempt an install-on-demand for sandbox users
        code, _, err = await run_shell("sudo apt-get install -y git || apt-get install -y git", timeout=120)
        if code != 0 and not _git_available():
            return {"status": "error", "error": f"git not installed ({err[:200]})"}

    action = arguments.get("action", "status")
    if action not in ALLOWED:
        return {"status": "error", "error": f"action must be one of {ALLOWED}"}

    cwd = arguments.get("cwd") or DEFAULT_WORKING_DIR
    cmd: list[str] = ["git", action]
    if action == "commit" and not arguments.get("allow_push") and "push" in str(arguments.get("message", "")):
        return {"status": "error", "error": "push is not allowed by this tool"}

    if action == "status":
        cmd = ["git", "status", "--short", "--branch"]
    elif action == "diff":
        cmd = ["git", "diff"] + (["--stat"] if arguments.get("stat_only") else [])
    elif action == "commit":
        message = str(arguments.get("message") or "")
        if not message:
            return {"status": "error", "error": "commit requires a message"}
        cmd = ["git", "add", "-A"] if arguments.get("add_all", True) else ["true"]
        add_cmd = " ".join(shlex.quote(c) for c in cmd)
        commit_cmd = "git commit -m " + shlex.quote(message)
        full = f"cd {shlex.quote(str(cwd))} && {add_cmd} && {commit_cmd}"
        code, stdout, stderr = await run_shell(full, timeout=60)
        return {"status": "success" if code == 0 else "error", "returncode": code,
                "output": maybe_truncate(stdout or stderr, 8000)}
    elif action == "log":
        limit = int(arguments.get("limit") or 20)
        cmd = ["git", "log", f"--max-count={limit}", "--oneline"]

    code, stdout, stderr = await run_shell("cd " + shlex.quote(str(cwd)) + " && " + " ".join(shlex.quote(c) for c in cmd), timeout=60)
    return {
        "status": "success" if code == 0 else "error",
        "returncode": code,
        "output": maybe_truncate(stdout, 16000) or maybe_truncate(stderr, 4000),
    }


TOOL = ToolSpec(
    name="git",
    description=(
        "Git operations inside the sandbox: status, diff (optionally --stat), "
        "commit (-A plus message; push is never allowed), log (--oneline with "
        "a max count). Installs git on demand if missing."
    ),
    input_schema={
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["status", "diff", "commit", "log"]},
            "message": {"type": "string", "description": "Commit message (action=commit)"},
            "stat_only": {"type": "boolean", "description": "action=diff: summary only"},
            "limit": {"type": "integer", "description": "action=log: commit count (default 20)"},
            "cwd": {"type": "string", "description": "Repo directory (default sandbox root)"},
        },
    },
    run=run_git,
)
