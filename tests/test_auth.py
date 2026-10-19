"""Phase 3 tests: token auth (HTTP + WebSocket), CORS, path safety.

The ASGI app is validated via TestClient. Auth state is read per-request
from the environment, so tests can monkeypatch SANDBOX_API_TOKEN freely.
"""


import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def token_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SANDBOX_API_TOKEN", "secret-token-123")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    from app import server

    # /healthz lazily starts a real Playwright browser — neuter it for tests
    async def _noop_initialize() -> None:
        return None

    monkeypatch.setattr(server.browser_manager, "initialize", _noop_initialize)
    monkeypatch.setattr(server.browser_manager, "status", "idle")
    return TestClient(server.app)


@pytest.fixture()
def client_no_token_config(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """No SANDBOX_API_TOKEN, but AUTH_DISABLED=true (must be explicitly set)."""
    monkeypatch.delenv("SANDBOX_API_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_DISABLED", "true")
    from app import server

    return TestClient(server.app)


@pytest.fixture()
def auth_disabled_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SANDBOX_API_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_DISABLED", raising=False)


# ---------------------------------------------------------------- HTTP


@pytest.mark.smoke
def test_healthz_exempt_from_auth(token_client: TestClient) -> None:
    resp = token_client.get("/healthz")
    assert resp.status_code == 200


@pytest.mark.smoke
def test_no_token_401(token_client: TestClient) -> None:
    # monkeypatch browser init away for determinism
    import app.server as srv

    async def _noop():
        return None

    storage = srv.browser_manager.initialize
    srv.browser_manager.initialize = _noop  # type: ignore[assignment]
    try:
        resp = token_client.get("/llm/providers")
        assert resp.status_code == 401
        assert "token" in resp.json()["detail"].lower()
    finally:
        srv.browser_manager.initialize = storage  # type: ignore[assignment]


@pytest.mark.smoke
def test_wrong_token_401(token_client: TestClient) -> None:
    resp = token_client.get("/llm/providers", headers={"x-sandbox-token": "wrong"})
    assert resp.status_code == 401


@pytest.mark.smoke
def test_valid_header_token_200(token_client: TestClient) -> None:
    resp = token_client.get("/llm/providers", headers={"x-sandbox-token": "secret-token-123"})
    assert resp.status_code == 200


@pytest.mark.smoke
def test_valid_bearer_token_200(token_client: TestClient) -> None:
    resp = token_client.get("/llm/providers", headers={"Authorization": "Bearer secret-token-123"})
    assert resp.status_code == 200


@pytest.mark.smoke
def test_fail_closed_without_config_and_without_auth_disabled(auth_disabled_env) -> None:
    from app.auth import ensure_server_auth_config

    with pytest.raises(RuntimeError, match="Refusing to start"):
        ensure_server_auth_config()


@pytest.mark.smoke
def test_auth_disabled_allows_unauthenticated(client_no_token_config: TestClient) -> None:
    resp = client_no_token_config.get("/llm/providers")
    assert resp.status_code == 200


@pytest.mark.smoke
def test_auth_disabled_requires_explicit_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_DISABLED", "yes")
    from app import auth

    assert auth.auth_disabled() is True


# ---------------------------------------------------------------- rate limit


@pytest.mark.smoke
def test_rate_limit_on_failed_attempts(token_client: TestClient) -> None:
    from app import auth as auth_module

    token_client.get("/llm/providers")  # 401 attempt #1
    for _ in range(auth_module._state.max_failures_per_window):
        token_client.get("/llm/providers", headers={"x-sandbox-token": "bad"})
    resp = token_client.get("/llm/providers", headers={"x-sandbox-token": "bad"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# ---------------------------------------------------------------- websocket


@pytest.mark.smoke
def test_ws_rejected_without_token(token_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), token_client.websocket_connect("/terminal"):
        pass


@pytest.mark.smoke
def test_ws_rejected_with_wrong_token(token_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), token_client.websocket_connect("/terminal?token=wrong"):
        pass


# ---------------------------------------------------------------- path safety


@pytest.mark.smoke
def test_pathsafety_semantics(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from app import pathsafety as ps

    monkeypatch.setenv("SANDBOX_ALLOWED_ROOTS", str(tmp_path))
    inside = ps.ensure_allowed(str(tmp_path / "sub" / "file.txt"))
    assert inside.is_absolute()
    # traversal outside root fails even with a sneaky prefix
    with pytest.raises(ps.PathNotAllowed):
        ps.ensure_allowed(str(tmp_path / ".." / "escape.txt"))
    # symlink escape resolved before check
    sneaky_link = tmp_path / "link"
    outside_target = tmp_path.parent / "outside_target.txt"
    outside_target.write_text("x", encoding="utf-8")
    sneaky_link.symlink_to(outside_target)
    with pytest.raises(ps.PathNotAllowed):
        ps.ensure_allowed(str(sneaky_link))


# ---------------------------------------------------------------- CORS


@pytest.mark.smoke
def test_cors_allowed_origins_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.auth import parse_cors_origins

    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert parse_cors_origins() == []
    monkeypatch.setenv("CORS_ORIGINS", " https://a.example , https://b.example , ")
    assert parse_cors_origins() == ["https://a.example", "https://b.example"]
