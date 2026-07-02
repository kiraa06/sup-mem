"""The one interface every backend implements (HANDOVER §6.1, I6).

Everything above this line — the hook, the MCP server, the manifest — depends ONLY on this
abstract class. Adding a new backend must not require touching any of them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from sup_mem.models import Hit, Metadata

# Called as progress(done, total) during a reindex so the CLI can render a bar.
ProgressCallback = Callable[[int, int], None]


class MemoryBackend(ABC):
    """Shared storage for both front-doors (the hook and the MCP tools), I1."""

    @abstractmethod
    def store(self, text: str, metadata: Metadata | None = None) -> str:
        """Persist a memory and return its id. Idempotent on ``(text, source)`` where the
        backend can manage it (``source`` is read from ``metadata['source']``)."""

    @abstractmethod
    def search(self, query: str, k: int, threshold: float) -> list[Hit]:
        """Return up to ``k`` hits with ``score >= threshold``, best first.

        ``score`` MUST be normalized to 0..1 so ``threshold`` is portable across backends
        (§6.1): BM25 backends squash their rank; cosine backends map naturally.
        """

    @abstractmethod
    def manifest(self, max_topics: int) -> list[str]:
        """Return a compact topic index, never longer than ``max_topics`` (§6.7)."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Liveness + config summary.

        Keys: ``backend`` (str), ``count`` (int), ``embedding`` (``{provider, model, dim}``
        for vector backends, else ``None``), plus any backend-specific extras.
        """

    @abstractmethod
    def reindex(self, progress: ProgressCallback | None = None) -> None:
        """Re-embed / rebuild the store. No-op for lexical backends; required for vector (I7)."""

    @abstractmethod
    def fetch(self, memory_ids: list[str]) -> dict[str, str]:
        """Return ``{id: text}`` for the ids that exist. Used by the outcome ledger's
        attribution (docs/PHASE6-LOOP.md, L5); missing ids are simply omitted."""

    @property
    def hook_safe(self) -> bool:
        """Whether the per-prompt hook may call ``search()`` without loading a model (I2).

        Lexical backends and remote-embedder vector backends are safe; an in-process embedder
        (fastembed) is not, so the hook skips Tier-2 for it. Defaults to True.
        """
        return True

    # --- Lifecycle (optional to override) -------------------------------------------------
    def close(self) -> None:  # noqa: B027
        """Release resources (DB handles, clients). Optional override; default no-op."""

    def __enter__(self) -> MemoryBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
