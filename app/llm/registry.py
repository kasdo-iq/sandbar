"""Provider registry: providers.yaml loading, routing, fallback and retry.

Config resolution order (later wins):
  1. providers.yaml (path from PROVIDERS_CONFIG env, default ./providers.yaml)
  2. Environment overrides: LLM_<NAME>_<FIELD> (e.g. LLM_GROQ_MODEL),
     plus LLM_DEFAULT_PROVIDER / LLM_FALLBACK.

API keys live in env vars only; YAML stores the *name* of the variable.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from anthropic import AuthenticationError as AnthropicAuthError
from anthropic import RateLimitError as AnthropicRateLimit
from openai import AuthenticationError as OpenAIAuthError

# exceptions the SDK exposes for rate limiting
from openai import RateLimitError as OpenAIRateLimit
from pydantic import BaseModel, Field

from app.llm.base import LLMProvider, LLMResponse, Message, StreamEvent
from app.logger import logger


class ProviderConfig(BaseModel):
    """One provider entry, as written under `providers:` in providers.yaml."""

    name: str
    api_family: str = "openai-compatible"  # openai-compatible | anthropic | gemini
    enabled: bool = True
    api_key_env: str = ""
    model: str = ""
    base_url: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float | None = 60.0
    extra_headers: dict[str, str] = Field(default_factory=dict)
    system_prompt: str | None = None


class RegistryConfig(BaseModel):
    default_provider: str = ""
    fallback: list[str] = Field(default_factory=list)
    retries_per_provider: int = 1
    backoff_seconds: float = 1.0
    providers: list[ProviderConfig] = Field(default_factory=list)


class LLMError(RuntimeError):
    """Raised when no provider could serve the request."""


def load_config(path: str | None = None) -> RegistryConfig:
    """Load providers.yaml + env overrides (env wins)."""
    cfg_path = path or os.getenv("PROVIDERS_CONFIG") or "providers.yaml"
    raw: dict[str, Any] = {}
    p = Path(cfg_path)
    if p.is_file():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = RegistryConfig(
        default_provider=raw.get("default_provider", ""),
        fallback=raw.get("fallback", []) or [],
        retries_per_provider=int(raw.get("retries_per_provider", 1)),
        backoff_seconds=float(raw.get("backoff_seconds", 1.0)),
        providers=[
            ProviderConfig(name=name, **(values or {}))
            for name, values in (raw.get("providers") or {}).items()
        ],
    )
    # ---- environment overrides ----
    if os.getenv("LLM_DEFAULT_PROVIDER"):
        cfg.default_provider = os.environ["LLM_DEFAULT_PROVIDER"]
    if os.getenv("LLM_FALLBACK"):
        cfg.fallback = [x.strip() for x in os.environ["LLM_FALLBACK"].split(",") if x.strip()]
    for prov in cfg.providers:
        prefix = f"LLM_{prov.name.upper()}_"
        if os.getenv(prefix + "MODEL"):
            prov.model = os.environ[prefix + "MODEL"]
        if os.getenv(prefix + "BASE_URL"):
            prov.base_url = os.environ[prefix + "BASE_URL"]
        if os.getenv(prefix + "ENABLED") is not None:
            prov.enabled = os.environ[prefix + "ENABLED"].lower() == "true"
        if os.getenv(prefix + "API_KEY") is not None:
            # direct env key override (name still comes from api_key_env)
            prov.api_key_env = prov.api_key_env or (prefix + "API_KEY")
        if os.getenv(prefix + "TEMPERATURE"):
            prov.temperature = float(os.environ[prefix + "TEMPERATURE"])
        if os.getenv(prefix + "MAX_TOKENS"):
            prov.max_tokens = int(os.environ[prefix + "MAX_TOKENS"])
        if os.getenv(prefix + "SYSTEM_PROMPT"):
            prov.system_prompt = os.environ[prefix + "SYSTEM_PROMPT"]
    return cfg


def build_provider(cfg: ProviderConfig) -> LLMProvider:
    """Instantiate the right provider class for a config entry."""
    if not cfg.api_key_env:
        raise LLMError(f"provider '{cfg.name}' has no api_key_env configured")
    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise LLMError(
            f"provider '{cfg.name}': env var {cfg.api_key_env} is not set — "
            f"add it to your .env / environment to use this provider"
        )
    common: dict[str, Any] = {
        "name": cfg.name,
        "api_key": key,
        "default_model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "timeout": cfg.timeout,
        "extra_headers": cfg.extra_headers,
        "system_prompt": cfg.system_prompt,
    }
    from app.llm.providers import (
        AnthropicProvider,
        GeminiProvider,
        OpenAICompatibleProvider,
    )

    fam = cfg.api_family
    if fam == "openai-compatible":
        return OpenAICompatibleProvider(base_url=cfg.base_url, **common)
    if fam == "anthropic":
        return AnthropicProvider(base_url=cfg.base_url, **common)
    if fam == "gemini":
        return GeminiProvider(**{k: v for k, v in common.items() if k != "base_url"})
    raise LLMError(f"provider '{cfg.name}': unknown api_family '{fam}'")


class LLMRegistry:
    """Holds provider instances; routes chat traffic with fallback + retry."""

    def __init__(self, config: RegistryConfig | None = None) -> None:
        self.config = config or load_config()
        self.providers: dict[str, LLMProvider] = {}
        self.construction_errors: dict[str, str] = {}
        for pc in self.config.providers:
            try:
                self.providers[pc.name] = build_provider(pc)
            except (LLMError, Exception) as exc:  # noqa: BLE001 - log & continue
                self.providers.pop(pc.name, None)
                self.construction_errors[pc.name] = str(exc)
                logger.warning(f"[llm] provider '{pc.name}' not available: {exc}")

    # ------------------------------------------------------------------

    def _route(self, provider: str | None, model: str | None) -> list[str]:
        """Return the ordered candidate list for this request."""
        primary = provider or self.config.default_provider
        order = [primary] if primary else []
        for fb in self.config.fallback:
            if fb and fb not in order:
                order.append(fb)
        if not order:
            raise LLMError(
                "no provider configured: set default_provider/fallback in "
                "providers.yaml or LLM_DEFAULT_PROVIDER"
            )
        return order

    def _provider_running(self, name: str) -> LLMProvider:
        prov = self.providers.get(name)
        if prov is None:
            raise LLMError(self.construction_errors.get(name, f"provider '{name}' not available"))
        return prov

    async def _invoke_chat(self, name: str, messages: list[Message], model: str | None, tools: list[dict[str, Any]] | None, opts: dict[str, Any]) -> LLMResponse:
        prov = self._provider_running(name)
        return await prov.chat(messages, model=model, tools=tools, **opts)

    async def chat(
        self,
        messages: list[Message],
        *,
        provider: str | None = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **opts: Any,
    ) -> LLMResponse:
        """Non-streaming chat with fallback list and per-provider retry."""
        order = self._route(provider, model)
        last_exc: BaseException | None = None
        for name in order:
            for attempt in range(self.config.retries_per_provider + 1):
                try:
                    return await self._invoke_chat(name, messages, model, tools, opts)
                except (OpenAIRateLimit, AnthropicRateLimit) as exc:
                    last_exc = exc
                    wait = self.config.backoff_seconds * (2**attempt)
                    logger.warning(f"[llm] '{name}' rate-limited, retry in {wait:.1f}s")
                    await asyncio.sleep(wait)
                except (OpenAIAuthError, AnthropicAuthError, LLMError) as exc:
                    # auth/config errors: no point retrying this provider
                    last_exc = exc
                    logger.warning(f"[llm] '{name}' unusable: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001 - other transient errors
                    last_exc = exc
                    wait = self.config.backoff_seconds * (2**attempt)
                    logger.warning(f"[llm] '{name}' error {type(exc).__name__}: {exc}; retry in {wait:.1f}s")
                    await asyncio.sleep(wait)
        raise LLMError(f"all providers failed (tried: {', '.join(order)}): {last_exc}")

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        provider: str | None = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        **opts: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming chat. Falls back *before* the first token is emitted;
        once streaming has started, errors are surfaced as error events."""
        order = self._route(provider, model)
        emitted = False
        for name in order:
            try:
                prov = self._provider_running(name)
                agen = prov.chat_stream(messages, model=model, tools=tools, **opts).__aiter__()
                async for event in agen:
                    emitted = True
                    yield event
                return
            except Exception as exc:  # noqa: BLE001
                if emitted:
                    yield StreamEvent(type="error", content=f"{name}: {exc}")
                    return
                logger.warning(f"[llm] stream '{name}' failed: {exc}; trying next provider")
                if name == order[-1]:
                    yield StreamEvent(type="error", content=f"all providers failed: {exc}")

    # ------------------------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """Provider listing for GET /llm/providers — never includes keys."""
        by_name = {p.name: p for p in self.config.providers}
        return [
            {
                "name": pc.name,
                "api_family": pc.api_family,
                "enabled": pc.enabled and pc.name in self.providers,
                "configured": pc.api_key_env in os.environ,
                "api_key_env": pc.api_key_env,
                "model": pc.model,
                "base_url": pc.base_url,
                "default": pc.name == self.config.default_provider,
                "error": self.construction_errors.get(pc.name),
            }
            for pc in sorted(by_name.values(), key=lambda x: x.name)
        ]


_instance: LLMRegistry | None = None


def get_registry(config: RegistryConfig | None = None) -> LLMRegistry:
    """Process-wide registry (lazy singleton; reload with reset_registry)."""
    global _instance
    if _instance is None or config is not None:
        _instance = LLMRegistry(config)
    return _instance


def reset_registry() -> None:
    """Forget the singleton (tests / config reload)."""
    global _instance
    _instance = None
