# AGENTS.md

Reconstructed Manus sandbox runtime (from bytecode). Python 3.11, FastAPI + uvicorn, Playwright Chromium, pexpect terminals.

## Run / verify

- Start server: `python start_server.py` (defaults: host `0.0.0.0`, port `8330`). Browser code lives behind `/browser/*`; terminal via WebSocket `/terminal`.
- Docker: `docker build -t manus-sandbox . && docker run -p 8330:8330 manus-sandbox` (base image: `mcr.microsoft.com/playwright/python:v1.50.0-noble`).
- No lint/typecheck/pytest config and no CI exist. Verify changes by running `pytest <file>`, importing modules, or hitting endpoints.
- Run a single test file: `pytest browser_use/agent/tests.py` (explicit paths work even without matching patterns).

## Testing quirks

- pytest only discovers `test_*.py` / `*_test.py` by default. Files named `tests.py` (`browser_use/agent/tests.py`, `browser_use/agent/message_manager/tests.py`) are NOT collected by bare `pytest` — pass their path explicitly.
- Tests/demos under `browser_use/dom/tests/process_dom_test.py` hit real websites (kayak.com etc.) with `headless=False` and are interactive (`input()` blocks) — never run them autonomously.
- `screenshot_test.py` needs Playwright browser binaries (`playwright install`) and network.
- Root `conftest.py` only puts the repo root on `sys.path` so `browser_use` / `app` imports resolve.

## Gotchas

- `requirements.txt` pins `browser-use==0.1.36`, but the local `browser_use/` directory is a heavily modified vendored copy that shadows the pip package (root-first sys.path). Always edit the local `browser_use/**`, never site-packages; don't "fix" imports to point at the pip version.
- `browser_use/browser/context.py` loads injection JS from repo-root `app_data/js/` via a `../../app_data/js` relative path — file moves break this silently.
- Playwright internals are imported (`playwright._impl._errors.TargetClosedError`) — upgrade Playwright only in lockstep with the pinned version.
- `app/tools/base.py`: `IS_INSIDE_CONTAINER` is determined by whether `/home/ubuntu` exists; it switches `DEFAULT_WORKING_DIR` and `DEFAULT_USER`. Behavior differs inside vs. outside Docker.
- Terminals: inside container they run `sudo su ubuntu` via pexpect; outside they run `/bin/bash --norc --noprofile`. Output parsing depends on a custom `[CMD_BEGIN]...[CMD_END]` prompt (PS1 set on spawn) — don't change PS1 logic casually.
- pexpect.spawn does not work natively on Windows; terminal features need Linux/WSL/Docker.
- `data_api.py`'s `ApiClient` constructor hard-depends on `$HOME/.secrets/sandbox_api_token`; host comes from `RUNTIME_API_HOST` (default `https://api.manus.im` — aim it at `http://localhost` for local testing).
- Env vars that matter: `CHROME_INSTANCE_PATH` (chrome path, also settable via `--chrome-path`), `RUNTIME_API_HOST`, `ANONYMIZED_TELEMETRY=false` to disable posthog telemetry, `BROWSER_USE_LOGGING_LEVEL=debug`, `WIN_FONT_DIR`.
- `browser_use/agent/service.py` calls `load_dotenv()` on import — a local `.env` affects agent code even when imported as a library.
- Error handling style: browser/text-editor endpoints return HTTP 200 with `{"status": "error", "error": ...}` bodies; file/terminal endpoints raise HTTPException. Check status fields, don't just look at status codes.

## Structure

- `app/` — the sandbox runtime API: `server.py` (all HTTP routes), `terminal_socket_server.py` (WebSocket), `tools/{browser,terminal,text_editor}`, Pydantic schemas in `types/` + `models.py`. Schema reference: `openapi.yaml` / `app/README.md`.
- `browser_use/` — vendored browser-use agent library (Claude-adapter agent, DOM building via `buildDomTree.js`, action registry).
- API surface the agent-facing tool layer uses: browser actions, terminals (execute/view/reset/kill), text editor, file upload/download to presigned S3 URLs, `/init-sandbox` (writes `$HOME/.secrets/*`).

## Conventions

- Several files have Chinese comments mixed with English — the project originates from Manus's production codebase; don't flag or rewrite wholesale.
- README.md describes auth (`x-sandbox-token`), a proxy, and rate limiting — none of it is implemented in this code; the FastAPI server has no auth and wide-open CORS (`allow_origins=["*"]`). Do not treat the README's security section as current behavior.
- Where docs conflict, trust the code: the editable sources of truth are `app/server.py` and `browser_use/**`.
