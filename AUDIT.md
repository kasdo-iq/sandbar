# AUDIT — Phase 0 (no code changes)

Date: 2026-10-20 · Scope: full read of `app/**`, `browser_use/**` entrypoints, `data_api.py`, `Dockerfile`, `requirements.txt`, docs.

## 1. Route map (`app/server.py`)

| Route | Method(s) | Handler | Wraps |
|---|---|---|---|
| `/healthz` | GET | `healthz` | lazily triggers `BrowserManager.initialize()` via fire-and-forget task when `status=="started"` |
| `/file/upload_to_s3` | POST | `upload_file` | reads local file, PUTs to presigned URL; >10MB returns `requires_multipart` info |
| `/file/multipart_upload_to_s3` | POST | `multipart_upload` | `upload_file_parts` concurrent part uploads |
| `/file` | GET | `get_file` | `FileResponse` for arbitrary path (query param) |
| `/request-download-attachments` | POST | `batch_download` | httpx GET list → `/home/ubuntu/upload/` |
| `/browser/status` | GET | `browser_status` | `BrowserManager.health_check()` |
| `/browser/action` | POST | `browser_action` | 60s `asyncio.wait_for` + `recreate_page` on timeout |
| `/text_editor` | POST | `text_editor_endpoint` | `TextEditor.run_action` |
| `/terminal` | WEBSOCKET | `websocket_endpoint` | `TerminalSocketServer.handle_connection` |
| `/terminal/{id}/reset`, `/reset-all` | POST | reset, reset_all | `TerminalManager` |
| `/terminal/{id}/view` | GET | view_terminal | `full: bool = Query(True)` |
| `/terminal/{id}/kill`, `/terminal/{id}/write` | POST | kill, write | pexpect-driven terminal |
| `/init-sandbox` | POST | init_sandbox | writes `$HOME/.secrets/<key>`, 0o600, timestamped backup |
| `/zip-and-upload` | POST | zip_and_upload | zip dir (excl. node_modules/.next/...) → PUT to presigned URL |

**TimedRoute (`app/router.py`):** custom `APIRoute` subclass; `app.router.route_class = TimedRoute` applies to *every* HTTP route. It logs `[>>] METHOD path` before and `[<<] ... in Xms` after each request. No auth, no validation beyond FastAPI defaults. Note: `WebSocket` routes bypass `TimedRoute` (it's an HTTP-routes feature).

## 2. Tool wiring (`app/tools/*`)

- `browser_manager = BrowserManager()` — module-level singleton instantiated at import of `server.py`. Lazy init on first `/browser/action` or `/healthz`; uses `CHROME_INSTANCE_PATH` env or `--chrome-path` argv (argv only sets the env in `start_server.py`).
- `terminal_manager` — singleton in `app/tools/terminal/__init__.py`; `Terminal` spawns `sudo su ubuntu` (in container) or `/bin/bash --norc --noprofile` (host) via `pexpect.spawn`, sets a `[CMD_BEGIN]...[CMD_END]` PS1 sentinel for output parsing; one-command-at-a-time (`is_running` guard).
- `text_editor` — singleton `TextEditor` in `app/tools/text_editor.py`; all I/O goes through `run_shell` (`cat`, `sudo cat`, `grep`, `find`, `ls`, `sudo mv`) — shell-quoting with single quotes, unsanitized → injection-prone.
- No registry exists; tools are hard-wired into `server.py` endpoints. Phase 4 adds one.

## 3. browser_use ↔ LLM

- `browser_use/agent/service.py: Agent` (vendored, heavily modified from browser-use 0.1.x, tabs+spaces mixed).
- LLM is passed in as a **LangChain `BaseChatModel`**; called via `self.llm.with_structured_output(self.AgentOutput, include_raw=True)` then `.ainvoke(...)` (`get_next_action`, ~line 392). Vision: screenshots passed as message content arrays.
- Special path: `deepseek-reasoner` / `deepseek-r*` → raw `llm.invoke` + JSON extraction (no function calling).
- Planner/page-extraction LLMs optional (`planner_llm`, `page_extraction_llm` default to main llm).
- Controller (`browser_use/controller/service.py`) holds a `Registry` of `@registry.action(...)` decorated actions; `BrowserAction` (`app/types/browser_types.py`) is a Pydantic **union-of-optionals** mirror — exactly one field set per request. `browser_manager.execute_action` finds the populated field, calls `controller.act(action, browser_context)`.
- Telemetry: `lmnr @observe` decorator (Laminar) + `ProductTelemetry` (PostHog). Both phone home by default. `ANONYMIZED_TELEMETRY=false` only kills PostHog; there is **no env** to disable Laminar short of removing the import.

## 4. `data_api.py`

- `ApiClient.__init__` unconditionally opens `$HOME/.secrets/sandbox_api_token` → **crashes if file missing**; host from `RUNTIME_API_HOST` (default `https://api.manus.im`).
- Not imported by the server; it's a client-side convenience. Will be wrapped in `MANUS_COMPAT` guard (default `true`) so import never explodes at module load.

## 5. Bugs found (verified by reading, not guessed)

1. **`multipart_upload` returns `response, 206` tuple** (`server.py` ~line 159). FastAPI serializes tuples as JSON arrays; the 206 status is never set. Should be `JSONResponse(status_code=206, ...)` — but changing response *type* must preserve shape for Manus compat, so fix carefully (body became `[model, 206]`). Marked for Phase 1+ fix with compat note.
2. **`upload_file_parts` arity mismatch**: `server.py` calls it with 3 args; `app/helpers/utils.py` defines a required 4th param `max_concurrent` → multipart uploads raise `TypeError` at runtime. Real crash bug.
3. **`TerminalInputMessage.create_response` requires `sub_command_index`** (no default) but every caller in `terminal_socket_server.py` omits it → `TypeError` on every WS action (reset/view/kill/…). The run/command path in `terminal.execute_command` likely passes it correctly; the fast-paths do not.
4. **`TerminalOutputMessage.type` mismatch**: websocket `error` responses use `type="error"`, allowed by `TerminalOutputMessageType` — OK — but `TerminalInputMessage.type` Literal does not include all WS message types the server dispatches on (`reset`, `reset_all`, `view`, … are included; fine) — no bug, verified.
5. **`create_zip_archive` copy ignores nested dirs incorrectly**: outer loop `os.listdir` + `shutil.copytree(..., ignore=lambda x, y: ignores)` — the ignore callback ignores *evaluated lazily* and matches top-level names only; nested `node_modules` in subfolders are still copied despite the docstring. Functional, just leaky.
6. **`server.py /terminal/{id}/write` sleeps 1s** before returning — hard-coded latency, not a bug.
7. **`text_editor` `view_dir` unreachable via HTTP**: `TextEditorCommand` Literal excludes `view_dir`, so Pydantic rejects it before dispatch. Dead code path.
8. **Check-style "final result"**: endpoint `assert result.output` — would be stripped under `python -O`.

## 6. Dead code / cruft

- `browser_use/agent/service.py` `__init__` has a redundant `else` branch that constructs a second `Browser()` when neither browser nor context given (line ~114-116) — actually reachable but constructs browser twice? Verified: if `browser is None and browser_context is None`, first block already set `self.browser = Browser()` and then `if browser_context:` is False, `elif self.browser:` is True → second branch never runs. Code is dead but harmless.
- `browser_use/telemetry/views.py`, `views.py` shadowing, `browser_use/utils.py` fine.
- `openapi.yaml` is hand-written and only approximately matches the code (`TerminalApiResponse` for write/read, etc.). Missing: `206` semantics for multipart; must be preserved as-is and extended in Phase 5.
- `conftest.py` only adds project root to `sys.path`.
- `browser_use/agent/service.py` mentions `WIN_FONT_DIR`, `C:\Windows\Fonts` — Windows-only convenience.

## 7. Security problems

| # | Problem | Severity |
|---|---|---|
| S1 | **No authentication on any endpoint.** Anyone with network access can execute shell commands, read/write arbitrary files, run arbitrary JS in a browser. | Critical |
| S2 | **CORS `allow_origins=["*"]` with `allow_credentials=True`** — any origin can call APIs (browsers block credentialed wildcard in practice, but it signals intent). | High |
| S3 | **Path traversal**: `/file?path=`, text editor, `/browser` screenshot paths all take arbitrary absolute paths; `validate_path` only *rewrites* `/x` → `$HOME/x`, doesn't restrict beyond that. | Critical |
| S4 | **Passwordless sudo** for `ubuntu` user in Dockerfile + `sudo su ubuntu` terminals. Any tool escape = root. | High (by design for Manus compat; Phase 1 adds `ALLOW_SUDO` build arg) |
| S5 | **Shell injection** via single-quoted interpolation in text_editor (`grep -n '{regex}' '{path}'`, `find ... -name '{pattern}'`) — attacker-controlled regex with a `'` breaks out. | High |
| S6 | **Secrets written to `$HOME/.secrets/` via `/init-sandbox`** — unauthenticated anyone can write arbitrary files with predictable names. | Critical |
| S7 | **Telemetry egress** (PostHog, Laminar/lmnr) by default. | Medium |
| S8 | `run_extensionAction.js`, `buildDomTree.js` injected into arbitrary pages — fine inside sandbox, noted for completeness. | Info |
| S9 | `.secrets` backup files keep mode 0o600 but get timestamped names and are never cleaned. | Low |
| S10 | `/zip-and-upload`: no size cap on zipped output (memory + `shutil.make_archive` to `/tmp`). | Medium |

## 8. Compatibility invariants (must not break)

- Exact paths + request/response shapes above (`app/types/*`, `app/models.py`, `openapi.yaml`).
- `x-sandbox-token` header name reserved for Phase 3 (currently ignored — adding validation *is* the fix, Manus sends it anyway).
- `TimedRoute` logging behavior.
- Terminal WS JSON protocol (`TerminalInputMessage` / `TerminalOutputMessage`).
- `python start_server.py` argv contract (`--port/--host/--log-level/--chrome-path`).

## 9. Fix plan (feeds Phases 1–4)

- P1: fix bugs #1/#2/#3; split requirements; pytest smoke test; Dockerfile `ALLOW_SUDO` ARG.
- P3: `app/auth.py` dependency (`SANDBOX_API_TOKEN`, `Authorization: Bearer` + `x-sandbox-token`), WS token gate (1008 close), CORS allowlist, per-IP failed-auth rate limit, allowed-roots for `/file` + text editor.
- P2: `app/llm/` with a LangChain-compatible adapter so `browser_use` Agent keeps working via `with_structured_output`.
- P4: `app/tools/registry.py`, generic `/tools` endpoints, agent loop with SSE, `custom_tools/` auto-discovery.
