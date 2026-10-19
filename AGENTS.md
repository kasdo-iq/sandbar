# AGENTS.md

Manus-compatible sandbox runtime (bytecode reconstruction, modernized 2026-10; history in `AUDIT.md`, `README.md`). FastAPI + uvicorn on 8330, Playwright Chromium, pexpect terminals, vendored `browser_use` agent. Container targets Python 3.11; this checkout's venv is 3.12 — keep new code 3.11-compatible.

## Commands

- Server: `python start_server.py` (`--port/--host/--log-level/--chrome-path`). Fail-closed: needs `SANDBOX_API_TOKEN`, or `AUTH_DISABLED=true` for local dev.
- Tests (offline, no keys/network): `pytest tests/` → 43 tests; fast subset `pytest tests/ -m smoke` (35). Markers live in `pytest.ini`.
- On this machine use the repo venv: `.\.venv\Scripts\python -m pytest tests/` (test deps in `requirements-dev.txt`).
- Lint: `ruff check .` (`ruff.toml`: strict for modernized files, per-file relief for legacy reconstructed ones). Do NOT run `ruff format` — it churns the legacy sources.
- Docker base `mcr.microsoft.com/playwright/python:v1.64.0-noble` must stay in lockstep with the playwright pin in `requirements.txt`; `--build-arg ALLOW_SUDO=false` builds a hardened image. No CI exists: verify with pytest + ruff + hitting endpoints.

## Compatibility invariants (extend, never rename/drop)

- Legacy surface is the Manus contract: `/file*`, `/browser/*`, `/text_editor`, `/terminal` (WS), `/terminal/{id}/*`, `/init-sandbox`, `/zip-and-upload`, `/healthz`; models in `app/types/`, `app/models.py`; documented in `openapi.yaml` + `app/README.md`. Add endpoints and fields; do not change existing ones.
- `/terminal` is a WebSocket route, so it never appears in FastAPI's generated `app.openapi()` even though `openapi.yaml` documents it — update `openapi.yaml` by hand when adding routes.
- `browser_use/` is a heavily modified vendored copy that shadows the pip package (root-first `sys.path`). Edit `browser_use/**` only; the PyPI `browser-use` package is deliberately absent from `requirements.txt`.
- `browser_use/browser/context.py` loads injection JS from repo-root `app_data/js/` via a `../../app_data/js` relative path — moving files breaks it silently.
- Playwright internals are imported (`playwright._impl._errors.TargetClosedError`); upgrade Playwright only together with the Docker base tag.
- Terminals inside the container run `sudo su ubuntu` and parse output via the custom `[CMD_BEGIN]...[CMD_END]` PS1 set on spawn — don't touch that logic.

## Auth, paths, config

- Every endpoint except `GET /healthz` requires `x-sandbox-token` or `Authorization: Bearer` vs `SANDBOX_API_TOKEN` (fail-closed startup); per-IP 429 after 10 failed auths/60s. WS `/terminal` accepts a `?token=` fallback and closes **1008 before accept**, so a TestClient connect attempt raises rather than opens.
- Middleware order in `app/server.py`: CORS is added before `AuthMiddleware`, so preflight never hits auth; `CORS_ORIGINS` empty means no cross-origin access.
- `app/pathsafety.py: ensure_allowed` guards every file-touching endpoint; `app/server.py` first regresses Manus-style absolute paths (`/x` → `DEFAULT_WORKING_DIR/x`). Roots come from `SANDBOX_ALLOWED_ROOTS` (default workdir).
- Secrets stay in env / `.env` (gitignored; `browser_use` calls `load_dotenv()` on import, so a local `.env` affects agent code) and `providers.yaml` (gitignored). Committed templates are `.env.example` / `providers.example.yaml` — never put real values there.
- `providers.yaml`: `api_key_env` holds the *name* of the env var, never the key. `MANUS_COMPAT` (default true) gates the `data_api.py` proxy client, which needs `$HOME/.secrets/sandbox_api_token`.

## Tools / LLM layer

- Tool contract: `ToolSpec(name, description, input_schema, run)` in `app/tools/registry.py`; builtins in `app/tools/*`, custom ones in `custom_tools/*.py` exposing module-level `TOOL`. Terminal/browser/text_editor are adapters in `app/tools/adapters.py`; their HTTP endpoints are untouched by that wiring.
- `web_search` picks Tavily → Parallel (`PARALLEL_API_KEY`; `PARALLEL_SEARCH_MODE` validated against `turbo|fast|basic|advanced`, invalid warns and falls back to `fast`) → SearXNG (`SEARXNG_URL`); with none set it returns `status: "disabled"`, not an error. Every backend returns `{title, url, snippet}`.
- `get_browser_use_llm()` (`app/llm/browser_adapter.py`) returns a LangChain chat model for the vendored Agent; passing your own Claude instance still works. google-genai's `HttpOptions.timeout` is **int milliseconds** — the string form fails client construction.
- `python_exec` memory rlimit is POSIX-only (guarded on Windows); `web_fetch`/`http_download` block private/metadata ranges and re-validate every redirect hop.

## Testing quirks

- `pytest.ini` sets `testpaths = tests`, so bare `pytest` skips the legacy `browser_use/**/tests.py` files — pass those paths explicitly.
- `browser_use/dom/tests/process_dom_test.py` drives real sites with `headless=False` and blocks on `input()`; `screenshot_test.py` needs Playwright binaries + network. Never run these unattended.
- Importing `app.server` runs `TOOL_REGISTRY.register_builtins()` / `discover_custom_tools()`. `GET /healthz` starts a real Playwright init in a background task: monkeypatch `browser_manager.initialize`/`status` in fixtures or TestClient teardown hangs.
- Auth and settings are read per request, so `monkeypatch.setenv` is the way to test them.
- Error style differs by endpoint family: browser/text-editor return HTTP 200 with `{"status": "error", ...}`; file/terminal raise HTTPException. Check the payload, not just the status code.

## Platform gotchas (this checkout runs on Windows)

- pexpect does not work on Windows: terminals, and anything POSIX-shell based (`text_editor` shells out to `cat`/`find`), fail outside Linux/WSL/Docker even though the server itself starts fine.
- `app/tools/base.py` decides container vs host by whether `/home/ubuntu` exists; that switches `DEFAULT_WORKING_DIR` and `DEFAULT_USER`, so behavior differs inside Docker.
- Browser: bundled Playwright Chromium is launched by default. `BROWSER_HEADLESS=false` shows a window; `BROWSER_CDP_URL=http://localhost:9222` attaches to your own Brave/Chrome started with `--remote-debugging-port=9222`. Setting `CHROME_INSTANCE_PATH`/`--chrome-path` alone does NOT launch that binary (browser_use attaches over CDP on :9222), so don't uncomment its use in `BrowserConfig` — the Dockerfile sets it and would break.
- Playwright browser builds must match the installed playwright pin: `.\.venv\Scripts\python -m playwright install chromium` after a version bump, or launches fail with "Executable doesn't exist ... chromium_headless_shell-<build>".
- `GET /browser/status` returns `{"healthy": bool, "tabs": [{page_id,url,title}]}`; `tabs` used to be a boolean because `health_check()` called a nonexistent `evaluate_javascript` — treat empty `tabs` as "no usable browser".

## Structure

- `app/server.py` all HTTP routes · `app/auth.py` middleware · `app/pathsafety.py` · `app/terminal_socket_server.py` WS · `app/tools/**` (registry, adapters, tools) · `app/llm/**` providers · `app/agent/loop.py` agent loop behind `POST /agent/run` (SSE) · `app/types/` + `app/models.py` schemas.
- `browser_use/**` vendored agent library · `app_data/js/` required by it · `custom_tools/` drop-in tools · `tests/` pytest.

## Conventions

- Several files keep Chinese comments from the original Manus codebase — don't translate or rewrite wholesale.
- Where docs and code disagree, trust `app/server.py`, `app/auth.py`, `browser_use/**`.
