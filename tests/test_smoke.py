"""Phase 1 smoke test: the FastAPI app starts and /healthz responds.

Kept minimal: no LLM keys, no real browser, no terminals required.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.smoke
def test_app_imports() -> None:
    """Importing app.server must not raise (covers most of the import graph)."""
    from app import server

    assert server.app is not None


@pytest.mark.smoke
def test_healthz_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /healthz returns 200 and does not try to launch a real browser."""
    from app import server

    async def _noop_initialize() -> None:  # noqa: RUF029
        return None

    monkeypatch.setattr(server.browser_manager, "initialize", _noop_initialize)
    monkeypatch.setattr(server.browser_manager, "status", "idle")

    client = TestClient(server.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
