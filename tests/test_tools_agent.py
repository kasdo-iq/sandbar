"""Phase 4 tests: tool registry, new tools, agent loop, custom discovery."""

import asyncio

import pytest

from app.tools.registry import TOOL_REGISTRY, ToolSpec


@pytest.fixture(autouse=True)
def ensure_builtins() -> None:
    if not TOOL_REGISTRY.list():
        TOOL_REGISTRY.register_builtins()


# ---------------------------------------------------------------- registry


@pytest.mark.smoke
def test_all_builtin_tools_registered() -> None:
    names = {t.name for t in TOOL_REGISTRY.list()}
    assert {
        "text_editor", "terminal", "browser",
        "python_exec", "web_fetch", "web_search", "file_search",
        "git", "http_download", "process_manager",
    } <= names


@pytest.mark.smoke
def test_openai_function_format() -> None:
    fns = TOOL_REGISTRY.openai_functions(["web_fetch"])
    assert len(fns) == 1
    assert fns[0]["type"] == "function"
    assert fns[0]["function"]["name"] == "web_fetch"
    assert "url" in fns[0]["function"]["parameters"]["properties"]


@pytest.mark.smoke
def test_custom_tool_discovery(tmp_path) -> None:
    tool_file = tmp_path / "echo_tool.py"
    tool_file.write_text(
        """
from app.tools.registry import ToolSpec

async def _run(args):
    return {"status": "success", "echo": args.get("msg", "")}

TOOL = ToolSpec(name="echo_tool", description="test echo",
                input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
                run=_run)
""",
        encoding="utf-8",
    )
    loaded = TOOL_REGISTRY.discover_custom_tools(str(tmp_path))
    assert "echo_tool" in loaded
    tool = TOOL_REGISTRY.require("echo_tool")
    result = asyncio.run(tool.run({"msg": "hi"}))
    assert result == {"status": "success", "echo": "hi"}
    # cleanup so the singleton doesn't leak across tests
    TOOL_REGISTRY._tools.pop("echo_tool", None)
    TOOL_REGISTRY._sources.pop("echo_tool", None)


@pytest.mark.smoke
def test_custom_tool_broken_file_fails_soft(tmp_path) -> None:
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')", encoding="utf-8")
    loaded = TOOL_REGISTRY.discover_custom_tools(str(tmp_path))
    assert loaded == []


# ---------------------------------------------------------------- tools


@pytest.mark.smoke
def test_web_fetch_blocks_localhost() -> None:
    from app.tools.web_fetch import run_web_fetch

    result = asyncio.run(run_web_fetch({"url": "http://127.0.0.1:8330/healthz"}))
    assert result["status"] == "error" and "blocked" in result["error"]


@pytest.mark.smoke
def test_web_fetch_blocks_metadata_ip() -> None:
    from app.tools.web_fetch import run_web_fetch

    result = asyncio.run(run_web_fetch({"url": "http://169.254.169.254/latest/meta-data"}))
    assert result["status"] == "error" and "blocked" in result["error"]


@pytest.mark.smoke
def test_web_fetch_blocks_private_range() -> None:
    from app.tools.web_fetch import run_web_fetch

    result = asyncio.run(run_web_fetch({"url": "https://192.168.1.1/x"}))
    assert result["status"] == "error" and "blocked" in result["error"]


@pytest.mark.smoke
def test_web_search_disabled_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools import web_search as ws

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    result = asyncio.run(ws.run_web_search({"query": "test"}))
    assert result["status"] == "disabled"


@pytest.mark.smoke
def test_web_search_backend_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools import web_search as ws

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setenv("PARALLEL_API_KEY", "k")
    assert ws._pick_backend() == ("parallel", ws._parallel)
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert ws._pick_backend()[0] == "tavily"  # tavily keeps priority


@pytest.mark.smoke
def test_web_search_parallel_result_mapping() -> None:
    from app.tools.web_search import _parse_parallel_results

    data = {
        "results": [
            {"title": "T1", "url": "https://a.example", "excerpts": ["alpha text", "more"]},
            {"title": "T2", "url": "https://b.example", "excerpts": []},
            {"title": "over the limit", "url": "https://c.example", "excerpts": ["x"]},
        ]
    }
    parsed = _parse_parallel_results(data, 2)
    assert parsed == [
        {"title": "T1", "url": "https://a.example", "snippet": "alpha text"},
        {"title": "T2", "url": "https://b.example", "snippet": ""},
    ]


@pytest.mark.smoke
def test_python_exec_smoke() -> None:
    from app.tools.python_exec import run_python

    result = asyncio.run(run_python({"code": "print(1 + 1)"}))
    assert result["status"] == "success"
    assert result["stdout"].strip() == "2"
    assert result["returncode"] == 0


@pytest.mark.smoke
def test_python_exec_timeout() -> None:
    from app.tools.python_exec import run_python

    result = asyncio.run(run_python({"code": "import time; time.sleep(5)", "timeout": 1}))
    assert result["status"] == "error" and "timed out" in result["error"]


@pytest.mark.smoke
def test_process_manager_list_and_kill() -> None:
    from app.tools.process_manager import PROCESSES, run_process_manager

    listing = asyncio.run(run_process_manager({"action": "list"}))
    assert listing["status"] == "success"
    # kill-all on the (empty) tracker must not fail
    killed = asyncio.run(run_process_manager({"action": "kill"}))
    assert killed["status"] == "success" and killed["count"] == 0
    _ = PROCESSES


@pytest.mark.smoke
def test_git_tool_unavailable_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools import git_tool

    result = asyncio.run(git_tool.run_git({"action": "status"}))
    assert result["status"] in {"success", "error"}  # env-dependent, never crashes
    bad = asyncio.run(git_tool.run_git({"action": "push"}))
    assert bad["status"] == "error"


@pytest.mark.smoke
def test_file_search_globs_repo() -> None:
    from app.tools.file_search import run_file_search

    result = asyncio.run(run_file_search({"glob": "**/pytest.ini"}))
    assert result["status"] == "success"
    assert result["file_count"] >= 1


# ---------------------------------------------------------------- agent loop


@pytest.mark.smoke
def _register_echo_tool() -> None:
    """Register a minimal echo tool for agent-loop tests."""
    async def _run(args):
        return {"status": "success", "echo": args.get("msg", "")}

    import app.tools.registry as reg_module

    if "echo_tool" not in reg_module.TOOL_REGISTRY._tools:
        reg_module.TOOL_REGISTRY.register(
            ToolSpec(name="echo_tool", description="echo test", input_schema={"type": "object"}, run=_run),
            source="test",
        )


@pytest.mark.smoke
def test_agent_loop_with_stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the LLM registry: first call asks for a tool, second answers."""
    from app.agent import loop as agent_loop
    from app.llm.base import LLMResponse, ToolCall

    _register_echo_tool()
    calls = {"n": 0}

    class StubRegistry:
        config = type("Cfg", (), {"default_provider": "stub"})()

        async def chat(self, messages, *, provider=None, model=None, tools=None, **opts):
            calls["n"] += 1
            if calls["n"] == 1:
                return LLMResponse(
                    provider="stub",
                    model="stub",
                    content="",
                    tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"msg": "hi"})],
                )
            assert messages[-1].role == "tool"
            assert "hi" in messages[-1].content
            return LLMResponse(provider="stub", model="stub", content="Echo done.", tool_calls=[])

    monkeypatch.setattr(agent_loop, "get_registry", lambda: StubRegistry())

    async def run() -> list:
        return [e async for e in agent_loop.run_agent_loop(goal="test the echo tool", max_steps=5)]

    events = asyncio.run(run())
    types = [e["type"] for e in events]
    assert "step_start" in types and "tool_call" in types and "tool_result" in types
    assert types[-1] == "final_answer"
    assert events[-1]["content"] == "Echo done."


@pytest.mark.smoke
def test_agent_loop_respects_max_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import loop as agent_loop
    from app.llm.base import LLMResponse, ToolCall

    _register_echo_tool()

    class StubRegistry:
        config = type("Cfg", (), {"default_provider": "stub"})()

        async def chat(self, messages, *, provider=None, model=None, tools=None, **opts):
            return LLMResponse(
                provider="stub", model="stub", content="",
                tool_calls=[ToolCall(id="c1", name="echo_tool", arguments={"msg": "hi"})],
            )

    monkeypatch.setattr(agent_loop, "get_registry", lambda: StubRegistry())

    async def run() -> list:
        try:
            return [e async for e in agent_loop.run_agent_loop(goal="busywork", max_steps=2, total_timeout_seconds=30)]
        finally:
            TOOL_REGISTRY._tools.pop("echo_tool", None)
            TOOL_REGISTRY._sources.pop("echo_tool", None)

    events = asyncio.run(run())
    assert events[-1]["type"] == "error" and "max_steps" in events[-1]["content"]
