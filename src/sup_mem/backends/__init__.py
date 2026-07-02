"""Pluggable storage backends behind one interface (I6).

``get_backend`` is the single construction point used by the hook, the MCP server and the CLI.
Backend modules are imported LAZILY here so that:
  * the zero-optional-deps default install never imports ``qdrant_client`` / ``fastembed`` (I8),
  * the hook's hot path only pays for the backend it actually uses (I2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sup_mem.backends.base import MemoryBackend
    from sup_mem.config import Config


def get_backend(config: Config) -> MemoryBackend:
    """Construct the configured backend. Raises ``ValueError`` for an unknown name."""
    name = config.backend
    if name == "sqlite_fts":
        from sup_mem.backends.sqlite_fts import SqliteFtsBackend

        return SqliteFtsBackend(config)
    if name == "qdrant":
        from sup_mem.backends.qdrant import QdrantBackend

        return QdrantBackend(config)
    if name == "pgvector":
        # Documented v1 stub (§14): the interface is reserved; not implemented.
        raise NotImplementedError(
            "The pgvector backend is a documented v1 stub (§14). Use 'sqlite_fts' or 'qdrant'."
        )
    raise ValueError(f"Unknown backend {name!r}. Expected one of: sqlite_fts, qdrant, pgvector.")


__all__ = ["get_backend"]
