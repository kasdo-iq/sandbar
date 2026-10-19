# Manus Sandbox — personal agent runtime

A containerized sandbox environment that enables AI agents to interact with terminal environments, web browsers, files, and a growing set of tools — programmatically, over HTTP.

This repo started as a bytecode reconstruction of Manus's sandbox runtime ([gist](https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9), [background](https://github.com/whit3rabbit/manus-open/issues/1)). It has since been modernized into **your own agent runtime that is still a valid Manus-compatible sandbox**.

## What is Manus-compatible vs. what was added

**Untouched (Manus compatibility):**

- Every legacy endpoint and its request/response schemas: `/file*`, `/browser/*`, `/text_editor`, `/terminal` (WS) + `/terminal/{id}/reset|reset-all|view|kill|write`, `/init-sandbox`, `/zip-and-upload`, `/healthz` (see `openapi.yaml`)
- `browser_use/` vendored library (behavior, action registry, prompts)
- Terminal `[CMD_BEGIN]...[CMD_END]` parsing, `sudo su ubuntu` session, pexpect protocol
- `start_server.py` argv contract (`--port/--host/--log-level/--chrome-path`)
- `data_api.py` Manus proxy client (behind the `MANUS_COMPAT` env flag, default true)

**Added in this modernization (`phase 1`–`5` commits):**

1. **Dependency refresh** — everything current (2026-10 pins), `requirements.txt` rebuilt from verified imports, split `requirements-dev.txt`, Playwright pip/base-image kept in lockstep, `pytest` smoke setup.
2. **Multi-provider LLM layer** (`app/llm/`) — Groq, Gemini, OpenRouter, Anthropic/Claude **and any OpenAI-compatible service by config only**, with `providers.yaml` + env overrides, per-request provider/model override, fallback chains with retry/backoff, SSE streaming (`POST /llm/chat`), and an adapter (`app/llm/browser_adapter.py`) so the browser_use `Agent` can run on any configured provider while existing Claude usage keeps working unchanged.
3. **Token authentication** on **every endpoint** (`app/auth.py`): `x-sandbox-token` (Manus-compatible) or `Authorization: Bearer`, fail-closed startup, per-IP failed-auth rate limiting (429 + Retry-After), WebSocket handshake rejection with close code 1008, env-driven CORS allowlist replacing the old `allow_origins=["*"]`, and allowed-roots path safety for all file-touching endpoints.
4. **Tool registry + agent loop** — `GET /tools`, `POST /tools/{name}/run`, `POST /agent/run` (SSE). Wraps terminal/browser/text-editor *plus* new tools: `python_exec`, `web_fetch`, `web_search`, `file_search`, `git`, `http_download`, `process_manager`. Drop a file in `custom_tools/` to add your own at startup.
5. **Docs/ops** — updated `openapi.yaml` (+ security scheme), this README, `ruff.toml`, pytest suites (38 tests).

## Quick start

```bash
# 1. configure
cp .env.example .env          # fill in SANDBOX_API_TOKEN at minimum
cp providers.example.yaml providers.yaml   # configure at least one enabled provider

# 2. run locally (Linux/WSL; terminals require pexpect — POSIX)
export SANDBOX_API_TOKEN="a-long-random-string"
python start_server.py                     # defaults to 0.0.0.0:8330

# verify
curl -H "x-sandbox-token: $SANDBOX_API_TOKEN" http://localhost:8330/llm/providers
curl http://localhost:8330/healthz          # the only unauthenticated endpoint
curl -s http://localhost:8330/llm/providers  # 401 without token
```

Docker:

```bash
docker build -t manus-sandbox .                        # default ALLOW_SUDO=true
docker run -p 8330:8330 -e SANDBOX_API_TOKEN=<token> manus-sandbox

# hardened build without passwordless sudo:
docker build --build-arg ALLOW_SUDO=false -t manus-sandbox-hard .
```

## Authentication

- Header `x-sandbox-token: <token>` (Manus-compatible) **or** `Authorization: Bearer <token>`.
- Token source: env `SANDBOX_API_TOKEN`. Constant-time compare (`hmac.compare_digest`); tokens are never logged.
- Applied globally via middleware to every HTTP route **except** `GET /healthz`.
- **Fail closed**: without `SANDBOX_API_TOKEN` the server refuses to start unless you explicitly set `AUTH_DISABLED=true` (a loud CRITICAL warning is logged; only for local dev).
- WebSocket `/terminal`: token from the handshake headers or `?token=` query fallback; invalid → socket closed with code **1008** before accept.
- Failed-auth rate limit: >10 failures/60s per IP → `429` with `Retry-After`.

## LLM providers (`providers.yaml`)

Copy `providers.example.yaml` to `providers.yaml` (or set `PROVIDERS_CONFIG`). Real keys live **only in env vars**; YAML stores each provider's `api_key_env` (the *name* of its key variable). Per provider you can set: `enabled`, `api_key_env`, `model`, `base_url`, `temperature`, `max_tokens`, `timeout`, `extra_headers`, `system_prompt`.

```yaml
default_provider: groq
fallback: [gemini, openrouter]      # tried in order on rate-limit/errors
retries_per_provider: 1
providers:
  groq:
    api_family: openai-compatible   # one class reused for Groq/OpenRouter/custom
    api_key_env: GROQ_API_KEY
    model: llama-3.3-70b-versatile
    base_url: https://api.groq.com/openai/v1
  my_custom:                        # any OpenAI-compatible service by config only
    api_family: openai-compatible
    api_key_env: MY_CUSTOM_API_KEY
    model: my-model
    base_url: https://my-service.example.com/v1
```

Env overrides (win over YAML): `LLM_DEFAULT_PROVIDER`, `LLM_FALLBACK`, and per-provider `LLM_<NAME>_MODEL | _BASE_URL | _ENABLED | _API_KEY | _TEMPERATURE | _MAX_TOKENS | _SYSTEM_PROMPT`.

- `GET /llm/providers` — configured providers; never returns keys.
- `POST /llm/chat` — `{messages, provider?, model?, tools?, temperature?, max_tokens?, stream?}`; `stream=true` → SSE (`delta`, `tool_call`, `final`, `error`, terminated by `data: [DONE]`).
- Browser agent: `app/llm/browser_adapter.py: get_browser_use_llm()` returns a LangChain chat model wired to the configured provider (Groq/OpenRouter via ChatOpenAI; Gemini via Google's OpenAI-compatible endpoint; Claude via ChatAnthropic). Passing your own Claude instance to `Agent` still works exactly as before.

## Tools & agent loop

`GET /tools` lists every registered tool with its JSON Schema; `POST /tools/{name}/run` executes one (body = the arguments object, or `{"arguments": {...}}`).

Built-in tools: `text_editor`, `terminal`, `browser` (wrappers around the unchanged Manus endpoints), plus `python_exec` (subprocess with timeout + POSIX rlimit), `web_fetch` (SSRF-guarded, redirect re-validated, size/time caps), `web_search` (Tavily/Brave/SearXNG — self-disables without keys), `file_search`, `git`, `http_download`, `process_manager`.

**Custom tools**: drop a `.py` file in `custom_tools/` exposing `TOOL = ToolSpec(name=..., description=..., input_schema={...}, run=async fn)` — auto-discovered at startup (`CUSTOM_TOOLS_DIR` overrides the folder).

`POST /agent/run` runs the tool-calling loop:

```jsonc
{
  "goal": "Find the current FastAPI version and write it to notes.txt",
  "provider": "groq",          // optional override
  "model": "llama-3.3-70b-versatile",
  "tools": ["web_fetch", "text_editor"],  // optional subset
  "max_steps": 15,
  "total_timeout_seconds": 300
}
```

It streams SSE step events (`step_start`, `assistant_text`, `tool_call`, `tool_result`, `final_answer`, `error`), enforces `max_steps` + a total timeout, and truncates tool output before feeding it back to the model.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `SANDBOX_API_TOKEN` | auth token for every endpoint | **required** (or `AUTH_DISABLED=true`) |
| `AUTH_DISABLED` | explicitly run unauthenticated (loud warning) | `false` |
| `CORS_ORIGINS` | comma-separated CORS allowlist | empty (no cross-origin) |
| `MANUS_COMPAT` | enable the Manus proxy client in `data_api.py` | `true` |
| `RUNTIME_API_HOST` | Manus proxy host for `data_api.py` | `https://api.manus.im` |
| `PROVIDERS_CONFIG` | path to providers.yaml | `providers.yaml` |
| `LLM_DEFAULT_PROVIDER`, `LLM_FALLBACK` | routing overrides | — |
| `LLM_<NAME>_*` | per-provider overrides | — |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` | provider keys (referenced by `api_key_env`) | — |
| `TAVILY_API_KEY`, `BRAVE_API_KEY`, `SEARXNG_URL` | web_search backends | — |
| `SANDBOX_ALLOWED_ROOTS` | comma-separated file-access roots (traversal/symlink-escape guard) | working dir (`/home/ubuntu` in container) |
| `SANDBOX_ALLOW_SUDO` | set by Dockerfile from the `ALLOW_SUDO` build arg | `true` |
| `CUSTOM_TOOLS_DIR`, `AGENT_MAX_STEPS`, `AGENT_TOTAL_TIMEOUT`, `AGENT_SYSTEM_PROMPT`, `AGENT_OUTPUT_MAX_CHARS` | agent loop tuning | `custom_tools`, 15, 300, built-in, 16000 |
| `PYTHON_EXEC_TIMEOUT`, `PYTHON_EXEC_MAX_MEMORY_MB`, `WEB_FETCH_MAX_BYTES`, `WEB_FETCH_TIMEOUT`, `HTTP_DOWNLOAD_TIMEOUT` | tool limits | 60s, 1024MB, 5MB, 30s, 60s |
| `CHROME_INSTANCE_PATH`, `BROWSER_USE_LOGGING_LEVEL`, `ANONYMIZED_TELEMETRY` | browser/telemetry (browser_use lib) | see `.env.example` |

## Vendored browser-use decision

`requirements.txt` intentionally does **not** pin the PyPI `browser-use` package: the repo-root `browser_use/` directory is a heavily modified fork that shadows the pip package and is what the sandbox API depends on. Do not re-add it. See `AGENTS.md` for other internals gotchas.

## Security

1. **Token auth on everything except `/healthz`** — fail-closed startup, constant-time comparison, per-IP lockout on failed attempts.
2. **CORS allowlist** — `CORS_ORIGINS`; empty means no browser cross-origin access.
3. **Path safety** — `/file*`, `/text_editor`, `/zip-and-upload`, `/request-download-attachments` and the download tool only touch paths inside `SANDBOX_ALLOWED_ROOTS`; traversal and symlink escapes are rejected. Text-editor shell commands are `shlex.quote`-hardened (previously single-quote injection).
4. **SSRF protection** — `web_fetch`/`http_download` block loopback/RFC1918/link-local/metadata ranges and re-validate each redirect hop.
5. **Secrets handling** — keys only via env; `.env` and `providers.yaml` are git/docker-ignored; nothing is logged.
6. **Isolation & sudo** — Docker container isolation; passwordless sudo remains the Manus-compatible default (`ALLOW_SUDO=true` build arg) but can be removed with a hardened build. Caveat: some tool paths (`text_editor` sudo flag, terminal `sudo su ubuntu`) assume NOPASSWD sudo — see `Dockerfile`.
7. **Telemetry egress** — `ANONYMIZED_TELEMETRY=false` disables PostHog; Laminar only activates if `LAMINAR_API_KEY` is set (leave unset for no egress).

Remaining limitations worth knowing: the terminal WS protocol trusts its clients once authenticated; python_exec memory rlimit is POSIX-only; `docker build` should still be smoke-tested on your Docker host (see notes in the final status report).

## API reference

`app/README.md` (endpoint-by-endpoint, original) and `openapi.yaml` (now including the auth scheme and all new endpoints) are served-side references. `AUDIT.md` documents the phase-0 code audit findings.

## Acknowledgements

Reconstructed from bytecode with Claude 3.7's help. The browser_use component is a modified version of the MIT-licensed [browser-use](https://github.com/browser-use/browser-use) library.
