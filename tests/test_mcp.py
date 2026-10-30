"""MCP server tools: remember / recall + the control-surface descriptions (§6.5, I5)."""

from __future__ import annotations

from collections.abc import Callable

from sup_mem.config import Config
from sup_mem.mcp import server as mcp_server


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


def test_model_supplied_source_never_supersedes(make_config: Callable[..., Config]) -> None:
    # Two unrelated facts the model labels source="user": both must stay live. A bare specific
    # source made the second silently supersede the first (T3 supersession by source).
    import sqlite3

    cfg = make_config(backend="sqlite_fts")
    tools = mcp_server.MemoryTools(cfg)
    try:
        tools.remember("The staging cluster runs in eu-central-1.", source="user")
        tools.remember("The user prefers squash merges on feature branches.", source="user")
        tools.remember("Deploys freeze on Fridays after 3pm.", source="mcp:ops")  # no double ns
    finally:
        tools.close()
    conn = sqlite3.connect(str(cfg.db_path))
    rows = conn.execute("SELECT source, superseded_at FROM memories ORDER BY source").fetchall()
    conn.close()
    assert rows == [("mcp:ops", None), ("mcp:user", None), ("mcp:user", None)]  # all live


def test_remember_description_is_honest_about_source() -> None:
    desc = mcp_server.REMEMBER_DESCRIPTION.lower()
    assert "source" in desc and "never updates or replaces" in desc


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


def test_server_class_resolves_across_sdk_majors() -> None:
    """The SDK facade must resolve on whatever mcp is installed (1.x FastMCP / 2.x MCPServer).

    mcp 2.0 dropped ``mcp.server.fastmcp``, which killed `sup-mem serve` on fresh installs
    while CI stayed green (uv.lock pinned 1.x). This fails loudly if neither path exists.
    """
    cls = mcp_server._server_class()
    assert cls.__name__ in {"FastMCP", "MCPServer"}
    server = cls("probe")  # positional name works on both majors
    assert callable(server.tool) and callable(server.run)
