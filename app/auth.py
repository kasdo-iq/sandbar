"""Token authentication for every sandbox endpoint.

- Header `x-sandbox-token: <token>` (Manus-compatible) OR
  `Authorization: Bearer <token>`.
- Token source: env `SANDBOX_API_TOKEN`. Fail closed: if unset at startup,
  the server refuses to serve unless `AUTH_DISABLED=true` is explicitly set.
- Constant-time comparison (hmac.compare_digest); tokens are never logged.
- Per-IP rate limiting on failed attempts (in-memory sliding window).
- `applied` via ASGI middleware on the FastAPI app, exempting GET /healthz
  and OPTIONS preflight requests.
"""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

# ---------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------


def auth_disabled() -> bool:
    """AUTH_DISABLED=true explicitly turns auth off (loudly).

    Accepts true/1/yes (case-insensitive) as an explicit affirmative.
    """
    return os.getenv("AUTH_DISABLED", "false").strip().lower() in {"true", "1", "yes"}


def parse_cors_origins() -> list[str]:
    """CORS_ORIGINS env -> list. Empty/whitespace -> [] (no cross-origin)."""
    return [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]


def config_token() -> str:
    """Current SANDBOX_API_TOKEN ('' when unset). Read per-request."""
    return os.environ.get("SANDBOX_API_TOKEN", "")


def extract_token(headers: dict[str, Any] | Request) -> str:
    """Pull token from headers (x-sandbox-token or Authorization: Bearer)."""
    headers_map = getattr(headers, "headers", headers)
    get = headers_map.get

    def case(k: str) -> str | None:
        return get(k) or get(k.title()) or get(k.replace("-", "_").lower())

    token = case("x-sandbox-token")
    if token:
        return str(token).strip()
    authz = case("authorization") or ""
    if isinstance(authz, str) and authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return ""


class AuthError(Exception):
    """Raised when a request fails auth (message never contains secrets)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


class AuthState:
    """Shared auth config + failed-attempt tracking."""

    #: max failed attempts per IP inside the window before 429
    max_failures_per_window: int = 10
    #: sliding window length, seconds
    window_seconds: int = 60

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def record_failure(self, ip: str) -> float:
        """Record a failed attempt; returns seconds the IP is blocked for (0 = ok)."""
        now = time.time()
        dq = self._failures[ip]
        dq.append(now)
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()
        if len(dq) > self.max_failures_per_window:
            return self.window_seconds - (now - dq[0])
        return 0.0

    def failures(self, ip: str) -> int:
        now = time.time()
        dq = self._failures[ip]
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()
        return len(dq)


_state = AuthState()


def auth_state() -> AuthState:
    return _state


def ensure_server_auth_config() -> None:
    """Fail-closed startup check; raise to abort the process.

    - SANDBOX_API_TOKEN unset AND AUTH_DISABLED unset  -> refuse to start.
    - AUTH_DISABLED=true without a token               -> loud warning.
    """
    if config_token():
        return
    if auth_disabled():
        logger.critical(
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            "  AUTH_DISABLED=true and no SANDBOX_API_TOKEN: the sandbox"
            "  API is running COMPLETELY UNAUTHENTICATED. Do not expose."
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        )
        return
    raise RuntimeError(
        "Refusing to start: SANDBOX_API_TOKEN is not set. "
        "Provide it in the environment, or set AUTH_DISABLED=true explicitly "
        "for unauthenticated local use."
    )


def verify_request_token(request: Request) -> None:
    """Validate the request token or raise AuthError (401/429)."""
    ip = request.client.host if request.client else "unknown"
    if auth_disabled():
        return
    expected = config_token()
    if not expected:
        # fail closed: auth is required but not configured anywhere
        raise AuthError(503, "auth not configured (SANDBOX_API_TOKEN missing) and AUTH_DISABLED not set")

    supplied = extract_token(request.headers)
    if not supplied or not hmac.compare_digest(supplied, expected):
        blocked = _state.record_failure(ip)
        if blocked > 0:
            raise AuthError(429, f"too many failed auth attempts; retry in {int(blocked) + 1}s")
        raise AuthError(401, "invalid or missing sandbox token")

    # valid; clear prior failures for this IP
    _state._failures.pop(ip, None)


def verify_ws_token(headers: dict[str, Any], query_params: dict[str, Any]) -> None:
    """Validate a WebSocket connection token or raise AuthError."""
    if auth_disabled():
        return
    expected = config_token()
    if not expected:
        raise AuthError(503, "auth not configured (SANDBOX_API_TOKEN missing) and AUTH_DISABLED not set")
    supplied = extract_token(headers) or str(query_params.get("token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise AuthError(401, "invalid or missing sandbox token")


class AuthMiddleware(BaseHTTPMiddleware):
    """Applies token auth to every HTTP route except GET /healthz.

    Added by app/server.py. CORS middleware is added first (outermost) so
    OPTIONS preflight requests never reach this module.
    """

    EXEMPT_PREFIXES = ("/healthz",)

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        # never authenticate CORS preflight/top-of-funnel probes
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.method == "GET" and any(
            request.url.path == p or request.url.path.startswith(p + "/") for p in self.EXEMPT_PREFIXES
        ):
            return await call_next(request)
        try:
            verify_request_token(request)
        except AuthError as exc:
            headers = {"Retry-After": str(int(exc.detail.split("retry in ", 1)[1].split("s", 1)[0]) + 1)} if exc.status_code == 429 else {}
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)
        return await call_next(request)


# late import to avoid a cycle
from app.logger import logger
