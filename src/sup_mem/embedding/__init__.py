"""Embedding providers + auto-detection (§6.4). Added in Phase 3; not on the hook path (I2).

Only ``base`` (stdlib-light) is imported eagerly; ``providers`` (and any optional embedding
library) is imported lazily inside ``get_embedder`` so importing this package stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sup_mem.embedding.base import (
    HOOK_SAFE_PROVIDERS,
    Embedder,
    EmbeddingError,
    EmbeddingMeta,
    provider_is_hook_safe,
)

if TYPE_CHECKING:
    from sup_mem.config import Config

__all__ = [
    "HOOK_SAFE_PROVIDERS",
    "Embedder",
    "EmbeddingError",
    "EmbeddingMeta",
    "get_embedder",
    "provider_is_hook_safe",
]


def get_embedder(config: Config) -> Embedder:
    """Build the configured embedder. Raises ``EmbeddingError`` if none is configured."""
    from sup_mem.embedding import providers

    provider = config.embedding.provider
    if not provider:
        raise EmbeddingError(
            "No embedding provider configured. Run: sup-mem setup --backend qdrant"
        )
    spec = providers.SPEC_BY_NAME.get(provider)
    if spec is None:
        raise EmbeddingError(f"Unknown embedding provider {provider!r}.")
    model = config.embedding.model or spec.default_model
    build = getattr(providers, spec.build_attr)
    embedder: Embedder = build(config, model)
    return embedder
