# AGENTS.md

Manus-compatible sandbox runtime (reconstructed from bytecode, modernized 2026-10 in phases 1–5). Python 3.11 (container) / 3.12 (local venv), FastAPI + uvicorn, Playwright Chromium, pexpect terminals.

## Run / verify

- Start server: `python start_server.py` (defaults: host `0.0.0.0`, port `8330`; argv: `--port/--host/--log-level/--chrome-path`). Requires `SANDBOX_API_TOKEN` in env (or `AUTH_DISABLED=true`) — fail-closed startup.
- Tests: `pytest tests/` (38 tests; no network/LLM keys needed). Also `requirements-dev.txt` (pytest, pytest-asyncio, ruff) and `ruff.toml`.
- Docker: base image `mcr.microsoft.com/playwright/python:v1.64.0-noble` — MUST stay in lockstep with the playwright pin in requirements.txt. `ARG ALLOW_SUDO=true` (hardened build: `--build-arg ALLOW_SUDO=false`).
- Run a single legacy test file: `pytest browser_use/agent/tests.py` (explicit paths work even without matching patterns).

## Testing quirks

- Files named `tests.py` (`browser_use/agent/tests.py`, `browser_use/agent/message_manager/tests.py`) are NOT collected by bare `pytest` — pass their path explicitly.
- Tests/demos under `browser_use/dom/tests/process_dom_test.py` hit real websites (kayak.com etc.) with `headless=False` and are interactive (`input()` blocks) — never run them autonomously.
- `screenshot_test.py` needs Playwright browser binaries (`playwright install`) and network.
- Root `conftest.py` only puts the repo root on `sys.path` so `browser_use` / `app` imports resolve.
- Importing `app.server` triggers `TOOL_REGISTRY.register_builtins()`; `GET /healthz` fires a real Playwright init in a background task — `monkeypatch` `browser_manager.initialize`/`status` in test fixtures or TestClient teardown hangs.

## Gotchas

- The local `browser_use/` directory is a heavily modified vendored copy that shadows anything installed from PyPI (root-first sys.path). Always edit the local `browser_use/**`, never site-packages; the pip `browser-use` package is deliberately NOT in requirements.txt — do not re-add it.
- `browser_use/browser/context.py` loads injection JS from repo-root `app_data/js/` via a `../../app_data/js` relative path — file moves break this silently.
- Playwright internals are imported (`playwright._impl._errors.TargetClosedError`) — upgrade Playwright only in lockstep with the pinned version and Docker base tag.
- `app/tools/base.py`: `IS_INSIDE_CONTAINER` is determined by whether `/home/ubuntu` exists; it switches `DEFAULT_WORKING_DIR` and `DEFAULT_USER`. Behavior differs inside vs. outside Docker (e.g. `text_editor` shells out to `cat`/`find` — POSIX only).
- Terminals: inside container they run `sudo su ubuntu` via pexpect; outside they run `/bin/bash --norc --noprofile`. Output parsing depends on a custom `[CMD_BEGIN]...[CMD_END]` prompt (PS1 set on spawn) — don't change PS1 logic casually.
- pexpect.spawn does not work natively on Windows; terminal features need Linux/WSL/Docker (verified: the server itself runs fine on Windows; only POSIX-only shells/terminal features are unavailable).
- `data_api.py`'s `ApiClient` still reads `$HOME/.secrets/sandbox_api_token`; guarded by `MANUS_COMPAT` env (default `true`) with a clear RuntimeError when disabled or the file is missing.
- Env vars that matter: `SANDBOX_API_TOKEN`, `AUTH_DISABLED`, `CORS_ORIGINS`, `SANDBOX_ALLOWED_ROOTS`, `CHROME_INSTANCE_PATH` (also via `--chrome-path`), `RUNTIME_API_HOST`, `ANONYMIZED_TELEMETRY=false` (disables posthog), `BROWSER_USE_LOGGING_LEVEL`, `WIN_FONT_DIR`, and everything in `.env.example`.
- `browser_use/agent/service.py` calls `load_dotenv()` on import — a local `.env` affects agent code even when imported as a library.
- Error handling style: browser/text-editor endpoints return HTTP 200 with `{"status": "error", "error": ...}` bodies; file/terminal endpoints raise HTTPException. Check status fields, don't just look at status codes.

## Modernization additions (phases 1–5)

- **Auth on every endpoint except `GET /healthz`** (`app/auth.py`, ASGI middleware): `x-sandbox-token` or `Authorization: Bearer` vs env `SANDBOX_API_TOKEN`; per-IP 429 after 10 failed attempts/60s; WS `/terminal` auth pre-accept (close 1008); CORS allowlist from `CORS_ORIGINS`. Auth state is read per request → tests can `monkeypatch.setenv`.
- **LLM layer** `app/llm/`: providers.yaml (`api_key_env` stores the NAME of the env var, never a key) + `LLM_*` env overrides; default/fallback routing with retry/backoff; `POST /llm/chat` supports SSE. `get_browser_use_llm()` (`app/llm/browser_adapter.py`) returns a LangChain chat model for the vendored Agent. Note: google-genai's `HttpOptions.timeout` is int milliseconds.
- **Tool registry** `app/tools/registry.py`: `ToolSpec(name, description, JSON-schema input, async run)`; builtins (python_exec, web_fetch, web_search, file_search, git, http_download, process_manager + adapters wrapping terminal/browser/text_editor with unchanged HTTP endpoints) + `custom_tools/*.py` exposing module-level `TOOL`. Agent loop: `app/agent/loop.py` behind `POST /agent/run` (SSE, `max_steps` + total timeout + output truncation).
- **Path safety** (`app/pathsafety.py`): `SANDBOX_ALLOWED_ROOTS` (default `DEFAULT_WORKING_DIR`) enforced on all file-touching endpoints; traversal and symlink escapes rejected; absolute `/x` paths normalized to `$HOME/x` first (Manus-compat).
- **Lint**: `ruff.toml` — strict for modernized code, per-file relaxed for legacy reconstructed files; `ruff format` is intentionally NOT applied to legacy code.
- **Local secret files**: `.env` (gitignored, real keys) and `providers.yaml` (gitignored) exist locally; committed templates are `.env.example` / `providers.example.yaml`. Never put real keys in the examples.

## Structure

- `app/` — sandbox runtime API: `server.py` (all HTTP routes), `auth.py` (token middleware), `pathsafety.py`, `terminal_socket_server.py` (WebSocket), `tools/{browser,terminal,text_editor,registry,...}`, `llm/` (provider layer), `agent/` (loop + endpoints), Pydantic schemas in `types/` + `models.py`. Schema reference: `openapi.yaml` / `app/README.md`.
- `browser_use/` — vendored browser-use agent library (Claude-adapter agent, DOM building via `buildDomTree.js`, action registry).
- `custom_tools/` — drop-in tools (auto-discovered at startup).
- Tests: `tests/` (pytest), plus the legacy `browser_use/**/tests.py` files (explicit path only).

## Conventions

- Several files have Chinese comments mixed with English — the project originates from Manus's production codebase; don't flag or rewrite wholesale.
- Where docs conflict, trust the code: the editable sources of truth are `app/server.py`, `app/auth.py`, and `browser_use/**`.
- Legacy endpoints and their request/response schemas (`app/types/*`, `openapi.yaml` paths) are compatibility invariants — extend, don't change.
