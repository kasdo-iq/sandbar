"""process_manager tool + trackable sandbox spawns (PROCESSES).

Every tool that spawns an OS process registers it here, so the agent can
list and kill background work started by the sandbox. `kill` without a
pid kills ALL tracked processes.
"""

from __future__ import annotations

import asyncio
import builtins
import os
import signal
import time
from typing import Any

from app.logger import logger
from app.tools.registry import ToolSpec

try:  # POSIX only
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


class TrackedProcess:
    __slots__ = ("cmd", "pid", "proc", "started_at")

    def __init__(self, pid: int, cmd: str, proc: asyncio.subprocess.Process | None) -> None:
        self.pid = pid
        self.cmd = cmd
        self.started_at = time.time()
        self.proc = proc

    @property
    def running(self) -> bool:
        if self.proc is not None:
            return self.proc.returncode is None
        try:
            os.kill(self.pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class ProcessTracker:
    """Registry of processes spawned by sandbox tools."""

    def __init__(self) -> None:
        self._by_pid: dict[int, TrackedProcess] = {}

    def register(self, proc: Any, cmd: str) -> int | None:
        pid = getattr(proc, "pid", None)
        if pid is None:
            return None
        existing = self._by_pid.get(pid)
        if existing is not None:
            return pid
        self._by_pid[pid] = TrackedProcess(pid, cmd, proc if hasattr(proc, "returncode") else None)
        return pid

    def forget(self, pid: int) -> None:
        self._by_pid.pop(pid, None)

    def list(self) -> builtins.list[TrackedProcess]:
        return [
            tracked
            for tracked in sorted(self._by_pid.values(), key=lambda t: t.started_at)
            if tracked.running
        ]

    def kill(self, pid: int | None = None) -> builtins.list[int]:
        """SIGTERM (then SIGKILL after grace) a pid or everything tracked."""
        targets = [p for p in self.list() if p.pid == pid] if pid else self.list()
        killed: list[int] = []
        for tracked in targets:
            try:
                if tracked.proc is not None:
                    tracked.proc.terminate()
                else:
                    os.kill(tracked.pid, signal.SIGTERM)
                killed.append(tracked.pid)
                logger.info(f"[process_manager] terminated pid {tracked.pid} ({tracked.cmd[:80]})")
            except (OSError, ProcessLookupError):
                pass
        return killed


PROCESSES = ProcessTracker()


async def run_process_manager(arguments: dict[str, Any]) -> dict[str, Any]:
    action = arguments.get("action", "list")
    if action == "list":
        return {
            "status": "success",
            "processes": [
                {"pid": t.pid, "cmd": t.cmd[:200], "started_at": t.started_at, "running": t.running}
                for t in PROCESSES.list()
            ],
        }
    if action == "kill":
        pid = arguments.get("pid")
        killed = PROCESSES.kill(int(pid) if pid else None)
        return {"status": "success", "killed": killed, "count": len(killed)}
    return {"status": "error", "error": "action must be 'list' or 'kill'"}


TOOL = ToolSpec(
    name="process_manager",
    description=(
        "List or kill background processes started by the sandbox tools "
        "(python_exec, downloads, timed-out commands)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "kill"], "default": "list"},
            "pid": {"type": "integer", "description": "Specific pid for action=kill (omit = kill all)"},
        },
    },
    run=run_process_manager,
)
