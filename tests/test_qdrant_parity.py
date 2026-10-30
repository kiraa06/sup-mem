"""Qdrant backend parity + hook-path embedder bounds — hermetic: in-memory Qdrant, fake embedder."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from sup_mem.config import Config
from sup_mem.embedding.base import Embedder, EmbeddingMeta

qdrant_client = pytest.importorskip("qdrant_client")


class FakeEmbedder(Embedder):
    def __init__(self, model: str, dim: int) -> None:
        self._meta = EmbeddingMeta("ollama", model, dim)

    @property
    def meta(self) -> EmbeddingMeta:
        return self._meta

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            out.append([b / 255.0 + 0.01 for b in digest[: self._meta.dim]])
        return out


@pytest.fixture
def qdrant(
    make_config: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str, int], Any]]:
    from sup_mem.backends.qdrant import QdrantBackend

    shared = qdrant_client.QdrantClient(location=":memory:")
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda **_kw: shared)
    opened: list[Any] = []

    def open_backend(model: str, dim: int) -> Any:
        cfg = make_config(
            backend="qdrant",
            qdrant={"url": "http://unused:6333", "collection": "parity"},
            embedding={"provider": "ollama", "model": model},
        )
        backend = QdrantBackend(cfg)
        backend._embedder_cache = FakeEmbedder(model, dim)
        opened.append(backend)
        return backend

    yield open_backend


def _live_texts(backend: Any) -> list[str]:
    return sorted(str((p.payload or {}).get("text")) for p in backend._iter_payloads())


def test_specific_source_supersedes_like_sqlite(qdrant: Callable[[str, int], Any]) -> None:
    backend = qdrant("fake", 8)
    backend.store("Deploys use the old rsync script.", {"source": "native:p/deploy.md"})
    backend.store("Deploys use the blue-green pipeline.", {"source": "native:p/deploy.md"})
    assert _live_texts(backend) == ["Deploys use the blue-green pipeline."]  # one live version

    backend.store("first independent remark", {"source": "mcp:user"})
    backend.store("second independent remark", {"source": "mcp:user"})
    assert len(_live_texts(backend)) == 3  # generic buckets never supersede


def test_reindex_survives_an_embedding_dimension_change(
    qdrant: Callable[[str, int], Any], make_config: Callable[..., Config]
) -> None:
    old = qdrant("fake", 8)
    for i in range(3):
        old.store(f"memory number {i} about deploy pipelines", {"source": f"s{i}"})
    new = qdrant("fake-large", 16)  # the user switched models: 8 → 16 dims
    new.reindex()  # used to fail: "could not broadcast (16,) into (8,)"
    assert len(_live_texts(new)) == 3
    assert new._read_stored_meta().dim == 16
    assert new.search("deploy pipelines", k=3, threshold=0.0)  # vectors queryable again
    dumps = list(make_config().backups_dir.glob("qdrant-parity-*.jsonl"))
    assert len(dumps) == 1 and len(dumps[0].read_text().splitlines()) == 3  # safety copy


def test_hook_bounds_http_embedder_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    from sup_mem.embedding import providers

    monkeypatch.setattr(providers, "_embed_timeout", providers._EMBED_TIMEOUT)
    monkeypatch.setattr(providers, "_max_retries", 2)
    seen: list[float] = []

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(_req: object, timeout: float) -> _Resp:
        seen.append(timeout)
        return _Resp()

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    providers._http_post_json("http://x/embed", {})
    providers.configure_for_hook(5.0)
    providers._http_post_json("http://x/embed", {})
    assert seen == [60.0, 5.0]
    assert providers._max_retries == 0
