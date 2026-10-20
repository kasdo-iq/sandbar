# Third-party notices

Sandbar is a modernization of a repository that was itself reconstructed from
Manus's sandbox container. The codebase therefore has three layers with
different provenance and different licensing. Read this before you redistribute
anything.

## At a glance

| Layer | Where | Origin | License |
|---|---|---|---|
| Original additions | `app/llm/`, `app/agent/`, `app/auth.py`, `app/pathsafety.py`, `app/tools/` (registry, endpoints, adapters and the tools written for it), `app/static/ui/`, `tests/`, `.github/`, project docs | this project (2026) | **MIT** — see `LICENSE` |
| Reconstructed sandbox | `app/server.py`, `app/types/`, `app/models.py`, `app/helpers/`, `app/terminal_socket_server.py`, the terminal / browser / text-editor implementations, `start_server.py`, `data_api.py` | Manus sandbox container, reconstructed from bytecode | **No license declared** |
| Vendored agent library | `browser_use/`, `app_data/js/` | fork of browser-use 0.1.x | **MIT** — © 2024 Gregor Zunic |

## 1. browser-use (MIT)

`browser_use/` is a heavily modified fork of
[browser-use](https://github.com/browser-use/browser-use). The upstream project
is MIT-licensed; the fork keeps that license. The full notice:

```text
MIT License

Copyright (c) 2024 Gregor Zunic

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Modifications in this repository are released under the same MIT terms.

## 2. Reconstructed Manus sandbox — no license declared

Provenance:

- Reconstruction gist: <https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9>
- Upstream mirror: <https://github.com/whit3rabbit/manus-open> — public, but
  **contains no LICENSE file**; the GitHub API reports `license: null`.
- That project's README states the code is "reconstructed from bytecode with
  Claude 3.7's help".

Consequences:

- No open-source license is granted for this layer. Absent a license, default
  copyright applies: the rights holder keeps all rights and redistribution is
  not permitted without permission.
- The MIT `LICENSE` in this repository deliberately does **not** cover this
  layer, and nothing here should be read as granting rights to it.
- Before publishing or redistributing this repository, establish the rights to
  this layer: ask the upstream author, obtain permission from the rights
  holder, or replace the layer with an independent implementation.
- Manus is a product and trademark of its owner. This project is not
  affiliated with, endorsed by, or sponsored by Manus, and does not use the
  name.

## 3. Original work covered by this repository's LICENSE

The `LICENSE` file (MIT, © 2026 kasdo-iq) covers the work added on top of the
reconstructed base:

- `app/llm/` — provider registry, OpenAI-compatible / Gemini / Anthropic
  clients, fallback chain, SSE streaming, LangChain browser adapter.
- `app/auth.py`, `app/pathsafety.py` — token auth middleware, CORS allowlist,
  failed-auth rate limiting, allowed-roots path checks.
- `app/tools/registry.py`, `app/tools/endpoints.py`, `app/tools/adapters.py`,
  and the tools written for the registry: `python_exec`, `web_fetch`,
  `web_search`, `file_search`, `git`, `http_download`, `process_manager`,
  `ssrf`.
- `app/agent/` — the tool-calling agent loop behind `POST /agent/run`.
- `app/static/ui/` — the single-page chat UI behind `GET /ui`.
- `tests/`, `.github/workflows/ci.yml`, `README.md`, `README.ar.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `THIRD_PARTY_NOTICES.md`, and the
  additions to `AGENTS.md` / `openapi.yaml`.

## 4. Python dependencies

Runtime requirements are pinned in `requirements.txt`. They are separate
projects under their own licenses (mostly MIT, BSD-3-Clause or Apache-2.0);
consult each project for the exact terms. Nothing in this repository's LICENSE
alters those terms.

## 5. Trademarks

"Manus" is used only to describe API compatibility and provenance. All other
product names are trademarks of their respective owners.
