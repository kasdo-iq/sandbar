"""Multi-provider LLM layer.

- base.py: normalized message/tool-call models + `LLMProvider` ABC.
- providers/: one class per API family (OpenAI-compatible, Anthropic, Gemini).
- registry.py: providers.yaml loading, routing, fallback and retry.
- endpoints.py: GET /llm/providers and POST /llm/chat (SSE streaming).
- browser_adapter.py: LangChain model factory for the vendored browser_use Agent.

Secrets never live in YAML: providers.yaml only stores the NAME of the env
var holding the key (api_key_env).
"""
