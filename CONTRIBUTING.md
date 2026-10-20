# Contributing

Thanks for taking a look. Small, focused changes are the easiest to review.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env                 # set SANDBOX_API_TOKEN
cp providers.example.yaml providers.yaml
```

`pytest tests/` runs fully offline — no API keys and no network needed.
`pytest tests/ -m smoke` is the fast subset.

## Before you open a PR

- `pytest tests/` passes (57 tests at the time of writing).
- `ruff check .` is clean. **Do not run `ruff format`** — it rewrites the
  reconstructed legacy sources and buries the real change in noise.
- New code stays Python 3.11-compatible (the container targets 3.11).
- No secrets anywhere: not in code, tests, fixtures, logs, screenshots or docs.
  `.env` and `providers.yaml` stay local, always.
- Public API stability: do not rename or remove existing endpoints,
  `app/types/` models or `openapi.yaml` paths. Add instead of changing.

## Adding a tool

Drop a `.py` file into `custom_tools/` exposing a module-level `TOOL`:

```python
from app.tools.registry import ToolSpec

async def run(arguments: dict) -> dict:
    return {"ok": True, "echo": arguments.get("text", "")}

TOOL = ToolSpec(
    name="echo",
    description="Echo the given text back.",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    run=run,
)
```

It is discovered at startup and appears in `GET /tools`. Tools that belong to
the core set live in `app/tools/` and are listed in
`TOOL_REGISTRY._BUILTIN_TOOLS`.

## Commits

Prefixes like `feat:`, `fix:`, `docs:`, `test:`, `chore:`; one topic per commit;
imperative subject. Explain *why* in the body when it is not obvious.

## Licensing

This repository mixes three layers with different provenance — see
`THIRD_PARTY_NOTICES.md`. Contributions to the original layer are accepted under
the MIT license in `LICENSE`. Do not copy code from proprietary sources.
