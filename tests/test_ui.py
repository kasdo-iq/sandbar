"""Chat UI tests: GET /ui is public, everything the page calls is not.

`/healthz` and `/ui` are the only GET routes exempt from token auth. The UI
itself must keep the token out of the URL/localStorage and must talk to the
real endpoints, so these tests pin both the auth behaviour and that contract.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SANDBOX_API_TOKEN", "secret-token-123")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    from app import server

    # test_auth.py drives the shared failed-attempt counter to 429; start clean
    # so "no token" here means 401, not a leftover rate limit.
    from app.auth import auth_state

    auth_state()._failures.clear()

    async def _noop_initialize() -> None:
        return None

    # /healthz lazily starts a real Playwright browser — neuter it for tests
    monkeypatch.setattr(server.browser_manager, "initialize", _noop_initialize)
    monkeypatch.setattr(server.browser_manager, "status", "idle")
    return TestClient(server.app)


@pytest.mark.smoke
def test_ui_is_public(client: TestClient) -> None:
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Sandbox Agent" in resp.text


@pytest.mark.smoke
def test_ui_served_with_a_token_too(client: TestClient) -> None:
    resp = client.get("/ui", headers={"x-sandbox-token": "secret-token-123"})
    assert resp.status_code == 200


@pytest.mark.smoke
def test_agent_run_still_requires_token(client: TestClient) -> None:
    resp = client.post("/agent/run", json={"goal": "hello"})
    assert resp.status_code == 401


@pytest.mark.smoke
def test_ui_exemption_does_not_leak_to_other_routes(client: TestClient) -> None:
    for path in ("/tools", "/llm/providers", "/browser/status"):
        assert client.get(path).status_code == 401, path


@pytest.mark.smoke
def test_ui_keeps_token_in_session_storage_only(client: TestClient) -> None:
    body = client.get("/ui").text
    assert "x-sandbox-token" in body
    assert "sessionStorage.setItem" in body
    # the page must never persist the token outside the tab
    assert "localStorage.setItem" not in body
    assert "?token=" not in body
    assert "location.search" not in body


@pytest.mark.smoke
def test_ui_uses_the_real_agent_contract(client: TestClient) -> None:
    body = client.get("/ui").text
    # endpoints the page is allowed to call
    assert "'/agent/run'" in body
    assert "'/llm/providers'" in body
    # every SSE event the loop actually emits
    for event in ("step_start", "assistant_text", "tool_call", "tool_result", "final_answer"):
        assert event in body, event
    # no external assets/CDNs (works offline)
    assert "<script src" not in body
    assert "cdn." not in body
