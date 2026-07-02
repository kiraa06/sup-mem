"""Embedding interface + the I7 model-consistency contract (HANDOVER §6.4, §6.3).

An ``Embedder`` carries the ``(provider, model, dim)`` triple that vector backends persist and
enforce (I7). ``hook_safe`` tells the per-prompt hook whether it may use this embedder without
loading a model (I2): remote/HTTP embedders are thin calls to already-warm services; the
in-process ONNX embedder (fastembed) is reserved for the long-lived MCP server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# Providers the per-prompt hook may use without loading a model (I2). fastembed is excluded on
# purpose: it loads an ONNX model, so it belongs to the warm MCP server, not the short-lived hook.
HOOK_SAFE_PROVIDERS = frozenset({"ollama", "tei", "voyage", "openai"})


def provider_is_hook_safe(provider: str) -> bool:
    return provider in HOOK_SAFE_PROVIDERS


class EmbeddingError(RuntimeError):
    """Raised when no embedder can be resolved/reached, with remediation guidance."""


@dataclass(frozen=True)
class EmbeddingMeta:
    """The identity of the model that wrote/reads a vector store — the I7 contract triple."""

    provider: str
    model: str
    dim: int

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "dim": self.dim}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmbeddingMeta:
        return cls(str(data["provider"]), str(data["model"]), int(data["dim"]))

    def mismatch(self, other: EmbeddingMeta) -> bool:
        """Vectors are only comparable when provider+model+dim all match (I7)."""
        return (self.provider, self.model, self.dim) != (other.provider, other.model, other.dim)


class Embedder(ABC):
    # Overridden to False by in-process (model-loading) embedders like fastembed.
    hook_safe: bool = True

    @property
    @abstractmethod
    def meta(self) -> EmbeddingMeta:
        """Identity of this embedder ``(provider, model, dim)`` — may lazily probe ``dim``."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors (order-preserving)."""

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]
