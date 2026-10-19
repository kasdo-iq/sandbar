"""Tool registry: uniform tool declarations + runtime for the agent loop.

A tool is a `ToolSpec`: name, English description, JSON Schema for its
input, and an async `run(arguments) -> dict`.

- Existing terminal/browser/text-editor capabilities are wrapped here
  WITHOUT touching their HTTP endpoints (see adapters.py).
- Built-in tools (python_exec, web_fetch, ...) register at app startup.
- Anything dropped into `custom_tools/*.py` exposing a module-level
  `TOOL = ToolSpec(...)` is auto-discovered too (CUSTOM_TOOLS_DIR).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from app.logger import logger

DEFAULT_CUSTOM_TOOLS_DIR = "custom_tools"


@dataclass
class ToolSpec:
    """Declarative definition of one sandbox tool."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema object for run() args
    run: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

    def to_openai_function(self) -> dict[str, Any]:
        """OpenAI-compatible function entry (used by the LLM layer)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    """Name-keyed registry with builtin + custom-file discovery."""

    _BUILTIN_TOOLS: ClassVar[list[str]] = [
        "app.tools.python_exec",
        "app.tools.web_fetch",
        "app.tools.web_search",
        "app.tools.file_search",
        "app.tools.git_tool",
        "app.tools.http_download",
        "app.tools.process_manager",
        "app.tools.adapters",
    ]

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._sources: dict[str, str] = {}  # name -> where it came from
        self._builtins_registered = False

    def register(self, tool: ToolSpec, source: str = "builtin") -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"tool '{tool.name}' already registered from {self._sources[tool.name]}"
            )
        self._tools[tool.name] = tool
        self._sources[tool.name] = source

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool '{name}'")
        return tool

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def openai_functions(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        chosen = [self.require(n) for n in names] if names else self.list()
        return [t.to_openai_function() for t in chosen]

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def register_builtins(self) -> None:
        """Register tools shipped with the sandbox (idempotent)."""
        if self._builtins_registered:
            return
        for mod_name in self._BUILTIN_TOOLS:
            for spec in _tool_specs_from_module(mod_name):
                if spec.name not in self._tools:
                    self.register(spec, source="builtin")
        self._builtins_registered = True

    def discover_custom_tools(self, directory: str | None = None) -> list[str]:
        """Import every *.py in custom_tools/ exposing module-level `TOOL`."""
        base = Path(directory or os.getenv("CUSTOM_TOOLS_DIR", DEFAULT_CUSTOM_TOOLS_DIR))
        if not base.is_dir():
            base.mkdir(parents=True, exist_ok=True)
            (base / "README.md").touch()
            return []
        loaded: list[str] = []
        for py in sorted(base.glob("*.py")):
            if py.name.startswith("_"):
                continue
            modname = f"custom_tools_{py.stem}"
            try:
                spec = importlib.util.spec_from_file_location(modname, str(py))
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[modname] = module
                spec.loader.exec_module(module)
                tool = getattr(module, "TOOL", None)
                if isinstance(tool, ToolSpec) and tool.name not in self._tools:
                    self.register(tool, source=f"custom:{py.name}")
                    loaded.append(tool.name)
                elif isinstance(tool, ToolSpec) and tool.name in self._tools:
                    logger.warning(f"[tools] custom tool name collision: {tool.name}")
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill startup
                logger.error(f"[tools] failed to load custom tool {py}: {exc}")
        if loaded:
            logger.info(f"[tools] custom tools loaded: {loaded}")
        return loaded


def _tool_specs_from_module(module_name: str) -> list[ToolSpec]:
    """Import a builtin tool module and return its TOOL / TOOLS export."""
    module = importlib.import_module(module_name)
    specs = getattr(module, "TOOLS", None) or getattr(module, "TOOL", None)
    tools = specs if isinstance(specs, list) else ([specs] if specs else [])
    return [t for t in tools if isinstance(t, ToolSpec)]


TOOL_REGISTRY = ToolRegistry()
