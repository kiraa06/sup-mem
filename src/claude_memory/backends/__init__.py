"""Pluggable storage backends behind one interface (I6).

``get_backend`` is the single construction point used by the hook, the MCP server and the CLI.
Backend modules are imported LAZILY here so that:
  * the zero-optional-deps default install never imports ``qdrant_client`` / ``fastembed`` (I8),
  * the hook's hot path only pays for the backend it actually uses (I2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_memory.backends.base import MemoryBackend
    from claude_memory.config import Config


def get_backend(config: Config) -> MemoryBackend:
    """Construct the configured backend. Raises ``ValueError`` for an unknown name."""
    name = config.backend
    if name == "sqlite_fts":
        from claude_memory.backends.sqlite_fts import SqliteFtsBackend

        return SqliteFtsBackend(config)
    if name in {"qdrant", "pgvector"}:
        # Wired up in Phase 3 (vector backend). Until then, fail explicitly rather than import
        # a module that does not exist yet (keeps the base type-check + install clean).
        raise NotImplementedError(
            f"The {name!r} backend arrives in Phase 3. "
            f"Install its extra first: pip install 'claude-memory[{name}]'."
        )
    raise ValueError(f"Unknown backend {name!r}. Expected one of: sqlite_fts, qdrant, pgvector.")


__all__ = ["get_backend"]
