"""The long-lived MCP server exposing `remember` / `recall` (HANDOVER §6.5, I5).

The two tool DESCRIPTIONS are the control surface: Claude decides when to call them purely from
these strings + the conversation + the injected context + the session manifest. They are
shipped essentially verbatim from the handover — edit with care.

Started via ``sup-mem serve``. The process is long-lived and holds the backend (and, for
vector backends, the in-process embedder) warm, so the per-prompt hook can borrow that warmth
instead of loading anything itself (I2). Also usable from Claude Desktop via MCP config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sup_mem.backends import get_backend
from sup_mem.config import load_config
from sup_mem.models import Metadata

if TYPE_CHECKING:
    from sup_mem.config import Config

REMEMBER_DESCRIPTION = (
    "Store a durable fact, decision, preference, or correction that should persist across "
    "future sessions. Call when the user says things like 'remember that…', 'we decided…', "
    "'going forward, always…', or states a stable fact about their systems/preferences. "
    "Do NOT call for transient, turn-specific details or things already obviously stored. "
    "Every call stores a new, independent memory: `source` is an optional provenance label "
    "only — it never updates or replaces an earlier memory, so a correction must be stated "
    "in full as its own fact."
)

RECALL_DESCRIPTION = (
    "Fallback retrieval from long-term memory. Relevant context is normally injected "
    "automatically each turn, so call this ONLY when: the user references prior work you lack "
    "context for (e.g. 'the fix we did', 'that ticket', possessives about past projects) AND "
    "the context already present this turn does not cover it — optionally guided by a topic "
    "from the session manifest. Pass a focused query. For incident RCA, pass as_of "
    "(YYYY-MM-DD or ISO timestamp) to ask what the store believed AT THAT TIME instead of now."
)


class MemoryTools:
    """Backend-holding implementation of the two tools; unit-testable without the MCP wire."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._backend = get_backend(config)

    def remember(self, text: str, tags: list[str] | None = None, source: str | None = None) -> str:
        text = text.strip()
        if not text:
            return "Nothing to store (empty text)."
        # ``source`` is model-supplied, so it is only ever a label: namespaced under ``mcp:`` it
        # lands in a generic bucket the backend never supersedes on. A bare specific source
        # (e.g. "user") made each new fact silently retire every live one with that label.
        label = (source or "").strip()
        if label and not label.startswith("mcp:"):
            label = f"mcp:{label}"
        metadata: Metadata = {"source": label or "mcp:remember"}
        if tags:
            metadata["tags"] = list(tags)
        mem_id = self._backend.store(text, metadata)
        return f"Stored durable memory (id {mem_id})."

    def recall(self, query: str, k: int | None = None, as_of: str | None = None) -> str:
        limit = k if (k and k > 0) else self._config.retrieval.k
        instant: str | None = None
        if as_of:
            from sup_mem.commands import _parse_as_of

            try:
                instant = _parse_as_of(as_of)
            except SystemExit as exc:
                return str(exc)
        threshold = self._config.retrieval.threshold
        try:
            if instant is None:
                # Mirror the hook: rank an unthresholded pool by outcome evidence FIRST, then
                # gate and cap. Gating first let chronic-ignore-demoted hits through and let
                # quarantine return fewer than k.
                pool_k = max(limit, self._config.ledger.pool_k)
                hits = self._backend.search(query, k=pool_k, threshold=0.0)
            else:
                hits = self._backend.search(query, k=limit, threshold=threshold, as_of=instant)
        except ValueError as exc:  # backend without version history + as_of (T6)
            return str(exc)
        if instant is None:
            try:
                from sup_mem.ranking import adjust

                hits = adjust(hits, self._config)  # outcome boost + quarantine (fail-open, L2)
            except Exception:
                pass
            hits = [h for h in hits if h.score >= threshold][:limit]
        if not hits:
            when = f" as of {instant[:19]}" if instant else ""
            return f"No stored memories matched that query{when}."
        header = (
            f"Memories as recorded at {instant[:19]} (beliefs THEN, possibly superseded since):"
            if instant
            else "Relevant long-term memories:"
        )
        lines = [header]
        for i, hit in enumerate(hits, 1):
            stamp = ""
            if instant:
                superseded = hit.metadata.get("_superseded_at")
                stamp = " [superseded since]" if superseded else " [still current]"
            lines.append(f"{i}. {hit.text}  (relevance {hit.score:.2f}){stamp}")
        return "\n".join(lines)

    def close(self) -> None:
        self._backend.close()


def _server_class() -> Any:
    """The MCP SDK's ergonomic server facade, across SDK majors.

    mcp 2.0 renamed ``FastMCP`` (``mcp.server.fastmcp``) to ``MCPServer``
    (``mcp.server.mcpserver``) and dropped the old module outright, which killed
    `sup-mem serve` on every fresh install (the dependency range allowed 2.x while the
    lockfile pinned 1.x for CI, so tests stayed green). Both classes take a positional
    name, a ``.tool(name=, description=)`` decorator, and ``.run()`` defaulting to stdio
    — our entire usage surface — so supporting both is a two-line import.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ModuleNotFoundError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP


def build_server(config: Config | None = None) -> Any:
    resolved = config or load_config()
    tools = MemoryTools(resolved)
    server = _server_class()("sup-mem")

    def remember(text: str, tags: list[str] | None = None, source: str | None = None) -> str:
        return tools.remember(text, tags=tags, source=source)

    def recall(query: str, k: int | None = None, as_of: str | None = None) -> str:
        return tools.recall(query, k=k, as_of=as_of)

    # Registered by call, not decorator syntax: the facade class is Any across SDK majors, and
    # an untyped decorator would strip these functions' own types under mypy --strict.
    server.tool(name="remember", description=REMEMBER_DESCRIPTION)(remember)
    server.tool(name="recall", description=RECALL_DESCRIPTION)(recall)

    return server


def serve(config: Config | None = None) -> None:
    """Run the long-lived stdio MCP server (used by Claude Code + Claude Desktop)."""
    build_server(config).run()


if __name__ == "__main__":
    serve()
