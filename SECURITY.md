# Security policy

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: **Security → Report a
vulnerability** on this repository. Do not open a public issue for anything
exploitable.

Include the affected endpoint or tool, a minimal reproduction, expected vs.
actual behaviour, and the commit you tested. If you need an encrypted channel,
say so in the report and we will arrange one.

We aim to acknowledge within 72 hours and to ship a fix or mitigation as soon
as practical. Please give us a chance to fix it before publishing details.

## In scope

- Authentication or authorization bypasses (`app/auth.py`, the WebSocket
  handshake, path safety).
- Path traversal and symlink escapes in file, editor, zip and download
  endpoints (`app/pathsafety.py`).
- SSRF in `web_fetch` / `http_download`, including redirect handling.
- Command injection in the terminal / text-editor tooling or `python_exec`.
- Secret leakage in responses, logs or error messages.

## Hard rules for operators

- **Never commit `.env` or `providers.yaml`.** They are git- and
  docker-ignored; keep them that way. If a key was ever committed, rotate it at
  the provider and purge it from history.
- `SANDBOX_API_TOKEN` grants full control of the sandbox — shell, files and
  browser. Treat it like an SSH key: long and random, rotated on suspicion, and
  never placed in a URL. The WebSocket accepts `?token=` for clients that
  cannot set headers; prefer the header.
- Bind to loopback (`--host 127.0.0.1`) or put the service behind a reverse
  proxy with TLS. The default `0.0.0.0` is intended for containers.
- Keep `AUTH_DISABLED` unset outside throwaway local development.
- The default container build keeps passwordless sudo for Manus compatibility
  (`ALLOW_SUDO=true`). Build with `--build-arg ALLOW_SUDO=false` when you do not
  need it.
- Set `CORS_ORIGINS` to an explicit allowlist; empty means no cross-origin
  access at all.
- `ANONYMIZED_TELEMETRY=false` (the `.env.example` default) plus no
  `LAMINAR_API_KEY` means no telemetry egress from the vendored agent.

## Known limitations

- The terminal WebSocket trusts an authenticated client for the whole session.
- `python_exec` memory limits are POSIX-only.
- Anything under `SANDBOX_ALLOWED_ROOTS` is readable by design. This is a
  trusted-operator sandbox, not a multi-tenant jail.

## License note

Parts of this repository are reconstructed from proprietary code and are not
licensed for redistribution — see `THIRD_PARTY_NOTICES.md` before publishing.
