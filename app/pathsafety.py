"""Path-safety helpers: restrict file access to configured allowed roots.

Used by /file*, /text_editor, /request-download-attachments and
/zip-and-upload. Rejected: absolute paths outside SANDBOX_ALLOWED_ROOTS,
`..` traversal, and symlink escapes (resolution happens before checks).

Env:
    SANDBOX_ALLOWED_ROOTS — comma-separated absolute dirs. Default: the
    effective working dir from app/tools/base.py (container: /home/ubuntu).
"""

from __future__ import annotations

import os
from pathlib import Path

from app.tools.base import DEFAULT_WORKING_DIR


class PathNotAllowed(Exception):
    """The requested path is not inside any allowed root."""


def allowed_roots() -> list[Path]:
    """Current allowed roots (re-read each call so tests can monkeypatch env)."""
    env = os.getenv("SANDBOX_ALLOWED_ROOTS", "")
    parts = [p.strip() for p in env.split(",") if p.strip()]
    if not parts:
        parts = [str(DEFAULT_WORKING_DIR)]
    return [Path(p).expanduser().resolve() for p in parts]


def ensure_allowed(path_str: str) -> Path:
    """Resolve `path_str` and verify it is inside an allowed root.

    Returns the resolved absolute Path; raises PathNotAllowed otherwise.
    """
    resolved = Path(path_str).expanduser()
    try:
        resolved = resolved.resolve(strict=False)
    except (OSError, ValueError):  # pragma: no cover - unusual paths
        resolved = Path(os.path.abspath(str(resolved)))
    for root in allowed_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PathNotAllowed(
        f"path '{path_str}' is outside the sandbox allowed roots"
    )
