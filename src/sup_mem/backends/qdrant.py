"""Opt-in vector backend: Qdrant + a pluggable embedder (HANDOVER §6.3, I7).

Embedding happens via the configured provider (§6.4). The per-prompt hook only calls this
backend when ``hook_safe`` is true (a remote/warm embedder), so the short-lived hook never
loads a model (I2); fastembed is used from the warm MCP server or batch paths.

I7 (model-consistency) is a hard contract: the ``(provider, model, dim)`` that wrote the store
is persisted as a reserved meta point INSIDE Qdrant, so it travels with the vectors. Any
attempt to read/write with a different model raises with a `reindex` remediation.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sup_mem.backends.base import MemoryBackend, ProgressCallback
from sup_mem.embedding import get_embedder
from sup_mem.embedding.base import (
    Embedder,
    EmbeddingError,
    EmbeddingMeta,
    provider_is_hook_safe,
)
from sup_mem.models import Hit, Metadata

if TYPE_CHECKING:
    from sup_mem.config import Config

_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")  # stable id namespace
_META_ID = str(uuid.uuid5(_NAMESPACE, "__sup_mem_meta__"))
_META_FLAG = "__cm_meta__"
_WORD_RE = re.compile(r"\w", re.UNICODE)
_REINDEX_HINT = "Run `sup-mem reindex` to re-embed the store with the current model."


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _point_id(text: str, source: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{source}\x00{text}"))


class QdrantBackend(MemoryBackend):
    def __init__(self, config: Config) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client import models as qm

        self._config = config
        self._qm = qm
        self._collection = config.qdrant.collection
        # Our API surface (collections/upsert/query_points/scroll/count) is stable across
        # server versions, so skip the client's strict minor-version compatibility warning.
        self._client = QdrantClient(url=config.qdrant.url, check_compatibility=False)
        self._embedder_cache: Embedder | None = None
        self._stored_meta: EmbeddingMeta | None = None

    # -- embedder + I7 --------------------------------------------------------------------
    def _embedder(self) -> Embedder:
        if self._embedder_cache is None:
            self._embedder_cache = get_embedder(self._config)
        return self._embedder_cache

    @property
    def hook_safe(self) -> bool:
        # Decided from config alone — never constructs the embedder (I2).
        return provider_is_hook_safe(self._config.embedding.provider)

    def _read_stored_meta(self) -> EmbeddingMeta | None:
        if self._stored_meta is not None:
            return self._stored_meta
        points = self._client.retrieve(self._collection, ids=[_META_ID], with_payload=True)
        if not points:
            return None
        payload = points[0].payload or {}
        self._stored_meta = EmbeddingMeta.from_dict(payload)
        return self._stored_meta

    def _configured_identity(self) -> tuple[str, str]:
        from sup_mem.embedding import providers

        provider = self._config.embedding.provider
        spec = providers.SPEC_BY_NAME.get(provider)
        model = self._config.embedding.model or (spec.default_model if spec else "")
        return provider, model

    def _verify_consistency(self) -> None:
        """Raise if the configured model differs from what wrote the store (I7)."""
        stored = self._read_stored_meta()
        if stored is None:
            return
        provider, model = self._configured_identity()
        if (stored.provider, stored.model) != (provider, model):
            raise EmbeddingError(
                f"Embedding-model mismatch (I7): store was written with "
                f"'{stored.provider}/{stored.model}' but config selects '{provider}/{model}'. "
                f"Vectors from different models are not comparable. {_REINDEX_HINT}"
            )

    def check_consistency(self) -> None:
        """Public I7 check for `doctor` — no-op if the store is empty/uninitialized."""
        if not self._client.collection_exists(self._collection):
            return
        self._verify_consistency()

    # -- collection lifecycle -------------------------------------------------------------
    def _create_collection(self, dim: int) -> None:
        qm = self._qm
        hnsw = self._config.qdrant.hnsw
        quant = None
        if self._config.qdrant.quantization:
            quant = qm.ScalarQuantization(
                scalar=qm.ScalarQuantizationConfig(type=qm.ScalarType.INT8, always_ram=True)
            )
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
            hnsw_config=qm.HnswConfigDiff(m=hnsw.m, ef_construct=hnsw.ef_construct),
            quantization_config=quant,
        )

    def _write_meta(self, meta: EmbeddingMeta) -> None:
        qm = self._qm
        payload = {**meta.as_dict(), _META_FLAG: True}
        self._client.upsert(
            self._collection,
            points=[qm.PointStruct(id=_META_ID, vector=[0.0] * meta.dim, payload=payload)],
        )
        self._stored_meta = meta

    def _ensure_writable(self) -> Embedder:
        embedder = self._embedder()
        if not self._client.collection_exists(self._collection):
            meta = embedder.meta
            self._create_collection(meta.dim)
            self._write_meta(meta)
        else:
            self._verify_consistency()
        return embedder

    def _not_meta(self) -> Any:
        qm = self._qm
        return qm.Filter(
            must_not=[qm.FieldCondition(key=_META_FLAG, match=qm.MatchValue(value=True))]
        )

    def initialize(self) -> EmbeddingMeta:
        """Create the collection (if needed) and record the embedding meta (I7); returns it.

        Called by `sup-mem setup` — constructs the embedder to learn the vector `dim`.
        """
        return self._ensure_writable().meta

    # -- writes ---------------------------------------------------------------------------
    def store(self, text: str, metadata: Metadata | None = None) -> str:
        text = text.strip()
        meta = dict(metadata or {})
        source = str(meta.get("source", ""))
        embedder = self._ensure_writable()
        vector = embedder.embed_query(text)
        point_id = _point_id(text, source)
        now = _now_iso()
        payload = {
            "text": text,
            "metadata": meta,
            "source": source,
            "created_at": now,
            "updated_at": now,
        }
        self._client.upsert(
            self._collection,
            points=[self._qm.PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return point_id

    # -- reads ----------------------------------------------------------------------------
    def search(self, query: str, k: int, threshold: float) -> list[Hit]:
        if k <= 0 or not _WORD_RE.search(query):
            return []
        if not self._client.collection_exists(self._collection):
            return []
        embedder = self._embedder()
        self._verify_consistency()
        vector = embedder.embed_query(query)
        result = self._client.query_points(
            self._collection,
            query=vector,
            limit=k,
            query_filter=self._not_meta(),
            search_params=self._qm.SearchParams(hnsw_ef=self._config.qdrant.hnsw.ef),
            with_payload=True,
        )
        hits: list[Hit] = []
        for point in result.points:
            score = max(0.0, min(1.0, float(point.score)))  # cosine → clamp to 0..1 (§6.1)
            if score < threshold:
                continue
            payload = point.payload or {}
            hits.append(
                Hit(
                    id=str(point.id),
                    text=str(payload.get("text", "")),
                    score=score,
                    metadata=dict(payload.get("metadata", {})),
                )
            )
        return hits

    # -- introspection --------------------------------------------------------------------
    def _iter_payloads(self) -> Any:
        offset = None
        while True:
            points, offset = self._client.scroll(
                self._collection,
                scroll_filter=self._not_meta(),
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            yield from points
            if offset is None:
                break

    def manifest(self, max_topics: int) -> list[str]:
        if max_topics <= 0 or not self._client.collection_exists(self._collection):
            return []
        from collections import Counter

        counter: Counter[str] = Counter()
        for point in self._iter_payloads():
            meta = (point.payload or {}).get("metadata", {})
            topic = meta.get("topic")
            if isinstance(topic, str) and topic.strip():
                counter[topic.strip()] += 1
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            if isinstance(tags, list):
                for tag in tags:
                    if str(tag).strip():
                        counter[str(tag).strip()] += 1
        return [topic for topic, _ in counter.most_common(max_topics)]

    def health(self) -> dict[str, Any]:
        exists = self._client.collection_exists(self._collection)
        count = 0
        stored = None
        if exists:
            count = int(
                self._client.count(
                    self._collection, count_filter=self._not_meta(), exact=True
                ).count
            )
            stored = self._read_stored_meta()
        return {
            "backend": "qdrant",
            "count": count,
            "embedding": stored.as_dict() if stored else None,
            "revision": f"{count}:{stored.model if stored else ''}",
            "url": self._config.qdrant.url,
            "collection": self._collection,
        }

    def reindex(self, progress: ProgressCallback | None = None) -> None:
        """Re-embed every memory with the CURRENT model and update stored meta (I7)."""
        if not self._client.collection_exists(self._collection):
            return
        embedder = self._embedder()
        meta = embedder.meta
        points = list(self._iter_payloads())
        total = len(points)
        for done, point in enumerate(points, 1):
            payload = point.payload or {}
            vector = embedder.embed_query(str(payload.get("text", "")))
            self._client.upsert(
                self._collection,
                points=[self._qm.PointStruct(id=point.id, vector=vector, payload=payload)],
            )
            if progress is not None:
                progress(done, total)
        self._write_meta(meta)  # store now matches the current model

    def close(self) -> None:
        self._client.close()
