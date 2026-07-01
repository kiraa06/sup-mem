"""Embedding-model consistency contract (HANDOVER §11.9, I7). Requires a live Qdrant."""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import Callable

import pytest

from claude_memory import commands
from claude_memory.backends import get_backend
from claude_memory.config import Config
from claude_memory.embedding.base import EmbeddingMeta

pytestmark = pytest.mark.qdrant


def _qdrant_cfg(make_config: Callable[..., Config], collection: str) -> Config:
    if (
        importlib.util.find_spec("qdrant_client") is None
        or importlib.util.find_spec("fastembed") is None
    ):
        pytest.skip("qdrant extra (qdrant-client + fastembed) not installed")
    return make_config(
        backend="qdrant",
        qdrant={
            "url": os.environ.get("QDRANT_URL", "http://localhost:6333"),
            "collection": collection,
        },
        embedding={"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
    )


def test_doctor_fails_on_model_mismatch_then_reindex_fixes(
    make_config: Callable[..., Config],
) -> None:
    collection = f"cm_i7_{uuid.uuid4().hex[:10]}"
    cfg = _qdrant_cfg(make_config, collection)
    backend = get_backend(cfg)
    try:
        backend.health()  # connectivity probe
    except Exception as exc:
        backend.close()
        pytest.skip(f"Qdrant not reachable: {exc}")

    try:
        backend.store("a durable fact worth remembering", {"source": "s"})
        stored = backend._read_stored_meta()  # noqa: SLF001
        assert stored is not None
        # Simulate: the store was written by a DIFFERENT model (model A).
        backend._write_meta(EmbeddingMeta("fastembed", "old-model-A", stored.dim))  # noqa: SLF001
        backend.close()

        # config selects bge-small but the store says old-model-A → doctor must fail (§11.9).
        assert commands.cmd_doctor(cfg) == 1
        # reindex re-embeds with the current model and rewrites the meta.
        assert commands.cmd_reindex(cfg) == 0
        # now consistent → doctor is green.
        assert commands.cmd_doctor(cfg) == 0
    finally:
        cleanup = get_backend(cfg)
        cleanup._client.delete_collection(collection)  # noqa: SLF001
        cleanup.close()
