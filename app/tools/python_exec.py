"""Run untrusted Python in a subprocess with hard limits.

Limits: CPU wall-clock timeout always; memory via `resource.setrlimit`
on POSIX only (no portable memory cap on Windows — documented).
Env: PYTHON_EXEC_TIMEOUT (default 60s), PYTHON_EXEC_MAX_MEMORY_MB (1024).
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from app.helpers.tool_helpers import maybe_truncate
from app.tools.base import DEFAULT_WORKING_DIR
from app.tools.process_manager import PROCESSES
from app.tools.registry import ToolSpec

try:  # POSIX-only memory limiting; absent on Windows (documented behavior)
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]


def _limits_preexec(memory_mb: int):  # pragma: no cover - runs in child process
    if resource is None:
        return None

    def preexec() -> None:
        try:
            _, hard = resource.getrlimit(resource.RLIMIT_AS)
            cap = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (min(cap, hard), hard))
        except (ValueError, OSError):
            pass

    return preexec


async def run_python(arguments: dict[str, Any]) -> dict[str, Any]:
    code = arguments.get("code", "")
    if not code.strip():
        return {"status": "error", "error": "code is required"}
    timeout = float(arguments.get("timeout") or os.getenv("PYTHON_EXEC_TIMEOUT", "60"))
    memory_mb = int(arguments.get("max_memory_mb") or os.getenv("PYTHON_EXEC_MAX_MEMORY_MB", "1024"))
    cwd = arguments.get("cwd") or DEFAULT_WORKING_DIR

    mem_limit = None if sys.platform == "win32" else _limits_preexec(memory_mb)
    cmd = [sys.executable, "-I", "-c", f"""import sys, json
_NS = {{}}
try:
    exec({code!r}, {{'__name__': '__main__'}}, {{}})
except SystemExit as _e:
    if _e.code: sys.stdout.write(str(_e.code))
except BaseException as _e:
    sys.stderr.write(f'{{type(_e).__name__}}: {{_e}}\\n')
    sys.exit(1)
"""]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=mem_limit,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"spawn failed: {exc}"}

    PROCESSES.register(proc, " ".join(cmd[:3]) or "python_exec")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        returncode = proc.returncode
        timed_out = False
    except TimeoutError:
        proc.kill()
        await proc.wait()
        stdout_b, stderr_b = b"", b""
        returncode, timed_out = -9, True
    finally:
        PROCESSES.forget(getattr(proc, "pid", 0))

    stdout = maybe_truncate(stdout_b.decode("utf-8", "replace"), 16000 + 5000)
    stderr = maybe_truncate(stderr_b.decode("utf-8", "replace"), 4000)
    if timed_out:
        return {"status": "error", "error": f"timed out after {timeout}s", "stdout": stdout}
    return {
        "status": "success" if returncode == 0 else "error",
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


TOOL = ToolSpec(
    name="python_exec",
    description=(
        "Execute Python source code in an isolated subprocess and capture "
        "stdout/stderr/returncode. Hard time limit and (on Linux) memory "
        "limit; the subprocess cannot outlive the call."
    ),
    input_schema={
        "type": "object",
        "required": ["code"],
        "properties": {
            "code": {"type": "string", "description": "Python source to exec"},
            "timeout": {"type": "number", "description": "Max seconds (default env PYTHON_EXEC_TIMEOUT)"},
            "max_memory_mb": {"type": "integer", "description": "RLIMIT_AS cap in MB (POSIX only)"},
            "cwd": {"type": "string", "description": "Working directory override"},
        },
    },
    run=run_python,
)
