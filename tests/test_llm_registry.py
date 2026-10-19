"""Phase 2 tests: LLM registry config loading, routing and endpoints.

No network calls: providers are built only when their key env var exists;
tests use monkeypatched keys and assert on structure, not live behavior.
"""

import pytest

from app.llm import registry as llm_registry
from app.llm.base import Message
from app.llm.registry import LLMError
from app.llm.registry import RegistryConfig, ProviderConfig, build_provider, get_registry, load_config


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch):
    llm_registry.reset_registry()
    yield
    llm_registry.reset_registry()


def test_load_config_missing_file_is_empty() -> None:
    cfg = load_config("/nonexistent/providers.yaml")
    assert cfg.providers == []


def test_load_config_full(tmp_path) -> None:
    f = tmp_path / "providers.yaml"
    f.write_text(
        """
default_provider: groq
fallback: [openrouter]
providers:
  groq:
    api_family: openai-compatible
    api_key_env: GROQ_API_KEY
    model: llama-3.3-70b-versatile
    base_url: https://api.groq.com/openai/v1
    extra_headers:
      X-Test: '1'
  openrouter:
    api_family: openai-compatible
    enabled: false
    api_key_env: OPENROUTER_API_KEY
    model: m
""",
        encoding="utf-8",
    )
    cfg = load_config(str(f))
    assert cfg.default_provider == "groq"
    assert [p.name for p in cfg.providers] == ["groq", "openrouter"]
    groq = cfg.providers[0]
    assert groq.api_key_env == "GROQ_API_KEY"
    assert groq.extra_headers == {"X-Test": "1"}
    assert cfg.providers[1].enabled is False


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    f = tmp_path / "providers.yaml"
    f.write_text(
        """
providers:
  groq:
    api_family: openai-compatible
    api_key_env: GROQ_API_KEY
    model: model-from-yaml
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_GROQ_MODEL", "model-from-env")
    monkeypatch.setenv("LLM_GROQ_ENABLED", "false")
    cfg = load_config(str(f))
    assert cfg.providers[0].model == "model-from-env"
    assert cfg.providers[0].enabled is False


def test_build_provider_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pc = ProviderConfig(name="groq", api_key_env="GROQ_API_KEY", model="m")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        build_provider(pc)


def test_registry_describe_no_keys(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    f = tmp_path / "providers.yaml"
    f.write_text(
        """
default_provider: groq
providers:
  groq:
    api_key_env: GROQ_API_KEY
    model: m
  claude:
    api_family: anthropic
    api_key_env: ANTHROPIC_API_KEY
    model: claude-sonnet-4-20250514
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GROQ_API_KEY", "fake-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    reg = get_registry(load_config(str(f)))
    listed = reg.describe()
    # a key must never leak into the listing
    assert "fake-key" not in str(listed)
    by_name = {p["name"]: p for p in listed}
    assert by_name["groq"]["enabled"] is True and by_name["groq"]["configured"] is True
    assert by_name["claude"]["enabled"] is False and by_name["claude"]["configured"] is False
    assert by_name["groq"]["default"] is True


def test_route_order_with_fallback(tmp_path) -> None:
    get_registry(
        RegistryConfig(
            default_provider="groq",
            fallback=["gemini", "groq", "claude"],
            providers=[
                ProviderConfig(name="groq", api_key_env="GROQ_API_KEY", model="m"),
                ProviderConfig(name="gemini", api_family="gemini", api_key_env="GEMINI_API_KEY", model="m"),
                ProviderConfig(name="claude", api_family="anthropic", api_key_env="ANTHROPIC_API_KEY", model="m"),
            ],
        )
    )
    reg = get_registry()
    assert reg._route(None, None) == ["groq", "gemini", "claude"]
    # explicit provider puts it first, then the configured fallback order
    assert reg._route("claude", None) == ["claude", "gemini", "groq"]


def test_chat_fails_without_providers(tmp_path) -> None:
    import asyncio

    get_registry(RegistryConfig(providers=[]))
    with pytest.raises(LLMError, match="no provider configured"):
        asyncio.run(get_registry().chat([Message(role="user", content="hi")]))


def test_openai_compat_provider_converts_tools() -> None:
    from app.llm.providers.openai_compat import OpenAICompatibleProvider

    conv = OpenAICompatibleProvider._convert_tools(
        [
            {"type": "function", "function": {"name": "a", "description": "d", "parameters": {}}},
            {"name": "b", "description": "d2", "parameters": {"type": "object"}},
        ]
    )
    assert conv[0]["function"]["name"] == "a"
    assert conv[1]["type"] == "function" and conv[1]["function"]["name"] == "b"
