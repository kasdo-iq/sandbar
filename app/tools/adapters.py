"""Wrappers that register the EXISTING sandbox capabilities as tools.

The HTTP endpoints in app/server.py are untouched — these adapters call
the same singletons (text_editor / browser_manager / terminal_manager)
through their public methods so behavior stays identical.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tools.base import ToolError
from app.tools.registry import ToolSpec
from app.tools.text_editor import text_editor
from app.tools.terminal import terminal_manager
from app.types.browser_types import BrowserAction
from app.types.messages import BrowserActionRequest, TerminalInputMessage, TextEditorAction


def _err(exc: Exception) -> dict[str, Any]:
    return {"status": "error", "error": str(exc)}


# ----------------------------------------------------------------------
# text editor tool (same shape as POST /text_editor)
# ----------------------------------------------------------------------


async def _text_editor_run(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await text_editor.run_action(TextEditorAction(**arguments))
        return {
            "status": "success",
            "result": result.output,
            "file_info": result.file_info.model_dump() if result.file_info else None,
        }
    except (ToolError, Exception) as exc:  # noqa: BLE001
        return _err(exc)


text_editor_tool = ToolSpec(
    name="text_editor",
    description=(
        "View, create, edit, replace and search files inside the sandbox. "
        "Same parameters as the POST /text_editor endpoint "
        "(command: view|create|write|str_replace|find_content|find_file)."
    ),
    input_schema={
        "type": "object",
        "required": ["command", "path"],
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "write", "str_replace", "find_content", "find_file"],
                "description": "Editor operation to run",
            },
            "path": {"type": "string", "description": "Absolute path inside the sandbox working dir"},
            "file_text": {"type": "string", "description": "Content for create/write"},
            "view_range": {"type": "array", "items": {"type": "integer"}},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "glob": {"type": "string"},
            "regex": {"type": "string"},
            "append": {"type": "boolean"},
        },
    },
    run=_text_editor_run,
)


# ----------------------------------------------------------------------
# terminal tool (drives the same pexpect session as /terminal/*)
# ----------------------------------------------------------------------


async def _terminal_run(arguments: dict[str, Any]) -> dict[str, Any]:
    terminal_id = arguments.get("terminal_id") or "main"
    command = arguments.get("command", "")
    try:
        terminal = await terminal_manager.create_or_get_terminal(terminal_id)
        if arguments.get("action") == "view":
            return {"status": "success", "output": terminal.get_history(True, True)}
        if arguments.get("action") == "kill":
            await terminal.kill_process()
            return {"status": "success", "output": terminal.get_history(True, False)}
        if not command:
            return {"status": "error", "error": "command is required"}
        msg = TerminalInputMessage(type="command", terminal=terminal_id, action_id="tool", command=command, mode="run")
        outputs = []
        async for out in terminal.execute_command(msg):
            outputs.extend(out.output)
        truncated = [str(o)[:16000] for o in outputs]
        return {"status": "success", "output": truncated, "terminal_id": terminal_id}
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


terminal_tool = ToolSpec(
    name="terminal",
    description=(
        "Run a shell command in the sandbox terminal session and wait for "
        "completion. action=view reads the session history; action=kill "
        "kills the currently running process."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "terminal_id": {"type": "string", "description": "Session name (default 'main')", "default": "main"},
            "action": {"type": "string", "enum": ["run", "view", "kill"], "default": "run"},
        },
    },
    run=_terminal_run,
)


# ----------------------------------------------------------------------
# browser tool (single action through the existing BrowserManager)
# ----------------------------------------------------------------------


async def _browser_run(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.server import browser_manager  # late import to dodge cycles

    try:
        action_body = arguments.get("action") or {}
        request = BrowserActionRequest(action=BrowserAction(**action_body))
        result = await asyncio.wait_for(browser_manager.execute_action(request), timeout=60)
        return {
            "status": "success",
            "url": result.url,
            "title": result.title,
            "result": result.result,
            "error": result.error,
            "elements_preview": str(result.elements)[:4000],
        }
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


browser_tool = ToolSpec(
    name="browser",
    description=(
        "Execute one browser automation action via Playwright "
        "(navigation, click, input text, screenshot, extract_content, ...). "
        "Pass an action object, e.g. {\"action\": {\"go_to_url\": {\"url\": \"https://example.com\"}}}."
    ),
    input_schema={
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "object",
                "description": (
                    "Exactly one browser_use action: go_to_url/click_element/"
                    "input_text/scroll_down/scroll_up/search_google/extract_content/"
                    "switch_tab/open_tab/go_back/"
                ),
            }
        },
    },
    run=_browser_run,
)

TOOLS = [text_editor_tool, terminal_tool, browser_tool]
