"""MCP server tools: remember / recall + the control-surface descriptions (§6.5, I5)."""

from __future__ import annotations

from collections.abc import Callable

from claude_memory.config import Config
from claude_memory.mcp import server as mcp_server


def test_remember_then_recall(make_config: Callable[..., Config]) -> None:
    tools = mcp_server.MemoryTools(make_config(backend="sqlite_fts"))
    try:
        msg = tools.remember(
            "We standardized on 100-char line length across all repos.",
            tags=["style", "convention"],
        )
        assert "stored" in msg.lower()
        out = tools.recall("what line length convention do we use")
        assert "100-char" in out
    finally:
        tools.close()


def test_recall_with_no_matches_returns_message(make_config: Callable[..., Config]) -> None:
    tools = mcp_server.MemoryTools(make_config(backend="sqlite_fts"))
    try:
        assert "no stored memories" in tools.recall("anything at all here").lower()
    finally:
        tools.close()


def test_remember_ignores_empty_text(make_config: Callable[..., Config]) -> None:
    tools = mcp_server.MemoryTools(make_config(backend="sqlite_fts"))
    try:
        assert "nothing to store" in tools.remember("   ").lower()
    finally:
        tools.close()


def test_tool_descriptions_are_the_control_surface() -> None:
    # I5: shipped roughly verbatim from §6.5 — these strings are how Claude decides to call.
    assert "durable fact" in mcp_server.REMEMBER_DESCRIPTION.lower()
    assert "remember that" in mcp_server.REMEMBER_DESCRIPTION.lower()
    assert "do not call" in mcp_server.REMEMBER_DESCRIPTION.lower()
    assert "fallback" in mcp_server.RECALL_DESCRIPTION.lower()
    assert "automatically" in mcp_server.RECALL_DESCRIPTION.lower()


async def test_server_registers_exactly_the_two_tools(make_config: Callable[..., Config]) -> None:
    server = mcp_server.build_server(make_config(backend="sqlite_fts"))
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {"remember", "recall"}
    by_name = {t.name: t for t in tools}
    assert by_name["remember"].description == mcp_server.REMEMBER_DESCRIPTION
    assert by_name["recall"].description == mcp_server.RECALL_DESCRIPTION
