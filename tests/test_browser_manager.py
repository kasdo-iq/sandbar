"""BrowserManager env-driven config: headless toggle and CDP attach.

No browser is launched here -- the constructor only reads the environment,
so these run offline and in milliseconds.
"""

from __future__ import annotations

import pytest


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)


@pytest.mark.smoke
def test_defaults_are_headless_without_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.browser.browser_manager import BrowserManager

    _clear(monkeypatch)
    manager = BrowserManager()
    assert manager.headless is True
    assert manager.cdp_url is None


@pytest.mark.smoke
def test_env_can_request_headed_and_cdp_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.browser.browser_manager import BrowserManager

    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://localhost:9222")
    manager = BrowserManager()
    assert manager.headless is False
    assert manager.cdp_url == "http://localhost:9222"


@pytest.mark.smoke
def test_explicit_argument_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.browser.browser_manager import BrowserManager

    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    assert BrowserManager(headless=True).headless is True


@pytest.mark.smoke
def test_health_check_lists_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.tools.browser.browser_manager import BrowserManager
    from browser_use.browser.views import TabInfo

    _clear(monkeypatch)
    manager = BrowserManager()

    class FakeContext:
        async def get_tabs_info(self) -> list[TabInfo]:
            return [TabInfo(page_id=1, url="https://example.com/", title="Example Domain")]

    assert asyncio.run(manager.health_check()) == []  # not ready yet

    manager.status = "ready"
    manager.browser, manager.browser_context, manager.controller = object(), FakeContext(), object()
    assert asyncio.run(manager.health_check()) == [
        {"page_id": 1, "url": "https://example.com/", "title": "Example Domain"}
    ]

    class BrokenContext(FakeContext):
        async def get_tabs_info(self) -> list[TabInfo]:
            raise RuntimeError("page dead")

    manager.browser_context = BrokenContext()
    assert asyncio.run(manager.health_check()) == []  # errors degrade to empty
