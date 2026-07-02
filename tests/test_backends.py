"""Backend conformance suite (HANDOVER §11.1) — runs against EVERY backend.

Phase 1 parametrizes only ``sqlite_fts``; Phase 3b appends ``qdrant`` to ``BACKEND_NAMES``
and the exact same assertions must pass there too (I6).
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import Callable, Iterator

import pytest

from sup_mem.backends import get_backend
from sup_mem.backends.base import MemoryBackend
from sup_mem.config import Config

# sqlite_fts always; qdrant is marked so `-m "not qdrant"` skips it and `-m qdrant` selects it.
BACKEND_PARAMS = [
    pytest.param("sqlite_fts"),
    pytest.param("qdrant", marks=pytest.mark.qdrant),
]

# Memories with clearly separable relevance so BM25 ordering is deterministic, not flaky.
SEED = [
    (
        "We migrated the authentication service from Flask to FastAPI last quarter.",
        {"tags": ["auth", "migration", "fastapi"], "source": "s1", "topic": "auth"},
    ),
    (
        "The Postgres connection pool size was raised to 50 to fix request timeouts.",
        {"tags": ["postgres", "performance"], "source": "s2", "topic": "database"},
    ),
    (
        "Kiran prefers tabs over spaces and a 100 character line length.",
        {"tags": ["preferences", "style"], "source": "s3", "topic": "preferences"},
    ),
    (
        "The nightly backup job writes snapshots to the archive bucket in S3.",
        {"tags": ["backup", "s3"], "source": "s4", "topic": "ops"},
    ),
]


@pytest.fixture(params=BACKEND_PARAMS)
def backend(
    request: pytest.FixtureRequest, make_config: Callable[..., Config]
) -> Iterator[MemoryBackend]:
    name: str = request.param
    if name == "qdrant":
        if (
            importlib.util.find_spec("qdrant_client") is None
            or importlib.util.find_spec("fastembed") is None
        ):
            pytest.skip("qdrant extra (qdrant-client + fastembed) not installed")
        url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        collection = f"cm_test_{uuid.uuid4().hex[:12]}"
        config = make_config(
            backend="qdrant",
            qdrant={"url": url, "collection": collection},
            embedding={"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
        )
        b = get_backend(config)
        try:
            b.health()  # forces a connection; skip cleanly if Qdrant isn't up
        except Exception as exc:
            b.close()
            pytest.skip(f"Qdrant not reachable at {url}: {exc}")
        try:
            yield b
        finally:
            b._client.delete_collection(collection)  # noqa: SLF001 — test cleanup
            b.close()
        return

    b = get_backend(make_config(backend=name))
    yield b
    b.close()


def _seed(b: MemoryBackend) -> None:
    for text, meta in SEED:
        b.store(text, meta)


def test_store_search_roundtrip(backend: MemoryBackend) -> None:
    _seed(backend)
    hits = backend.search("authentication service migration to fastapi", k=5, threshold=0.0)
    assert hits, "expected at least one hit"
    assert "authentication" in hits[0].text.lower()  # the most relevant memory ranks first


def test_search_respects_k(backend: MemoryBackend) -> None:
    _seed(backend)
    hits = backend.search("the service database backup preferences", k=2, threshold=0.0)
    assert len(hits) <= 2


def test_scores_normalized_0_to_1(backend: MemoryBackend) -> None:
    _seed(backend)
    hits = backend.search("postgres connection pool timeouts performance", k=5, threshold=0.0)
    assert hits
    assert all(0.0 <= h.score <= 1.0 for h in hits)


def test_scores_monotonic_with_relevance(backend: MemoryBackend) -> None:
    _seed(backend)
    hits = backend.search("postgres connection pool timeouts performance", k=5, threshold=0.0)
    # Best-first ordering: scores must be non-increasing.
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    # The postgres memory must out-score an unrelated one for a postgres query.
    by_text = {h.text: h.score for h in hits}
    postgres = next(s for t, s in by_text.items() if "postgres" in t.lower())
    others = [s for t, s in by_text.items() if "postgres" not in t.lower()]
    assert all(postgres >= o for o in others)


def test_threshold_filters(backend: MemoryBackend) -> None:
    _seed(backend)
    q = "authentication service migration to fastapi"
    low = backend.search(q, k=10, threshold=0.0)
    high = backend.search(q, k=10, threshold=0.99)
    assert len(high) <= len(low)
    assert all(h.score >= 0.99 for h in high)


def test_health_reports_count_and_embed_meta(backend: MemoryBackend) -> None:
    _seed(backend)
    health = backend.health()
    assert health["count"] == len(SEED)
    assert "backend" in health
    assert "embedding" in health  # None for lexical, a dict for vector backends


def test_manifest_never_exceeds_max_topics(backend: MemoryBackend) -> None:
    _seed(backend)
    assert len(backend.manifest(3)) <= 3
    assert len(backend.manifest(100)) <= 100
    assert backend.manifest(0) == []


def test_store_is_idempotent_on_text_and_source(backend: MemoryBackend) -> None:
    id1 = backend.store("a stable fact about the system", {"source": "same"})
    id2 = backend.store("a stable fact about the system", {"source": "same", "tags": ["x"]})
    assert id1 == id2
    assert backend.health()["count"] == 1  # no duplicate row


def test_fetch_roundtrips_and_omits_unknown_ids(backend: MemoryBackend) -> None:
    id1 = backend.store("alpha fact about deployment pipelines", {"source": "f1"})
    id2 = backend.store("beta fact about database tuning", {"source": "f2"})
    fetched = backend.fetch([id1, id2, "no-such-id"])
    assert fetched[id1].startswith("alpha fact")
    assert fetched[id2].startswith("beta fact")
    assert "no-such-id" not in fetched
    assert backend.fetch([]) == {}


def test_reindex_is_safe(backend: MemoryBackend) -> None:
    _seed(backend)
    backend.reindex()  # must not raise
    assert backend.search("postgres pool", k=3, threshold=0.0)  # still searchable


def test_empty_and_tokenless_queries(backend: MemoryBackend) -> None:
    # Universal across backends: no query tokens at all → no hits.
    _seed(backend)
    assert backend.search("", k=5, threshold=0.0) == []
    assert backend.search("!!! ??? ...", k=5, threshold=0.0) == []  # punctuation only, no words


def test_sqlite_nonmatching_terms_return_empty() -> None:
    # Lexical-only property (a vector backend returns nearest neighbours regardless): real
    # words absent from the store produce no FTS match.
    import tempfile

    from sup_mem.config import load_config

    with tempfile.TemporaryDirectory() as tmp:
        b = get_backend(load_config(overrides={"data_dir": tmp, "backend": "sqlite_fts"}))
        try:
            b.store("something about databases", {"source": "s"})
            assert b.search("zzzzzxxxxx qqqqwwwww vvvvbbbb", k=5, threshold=0.0) == []
        finally:
            b.close()


def test_default_install_needs_no_optional_deps(make_config: Callable[..., Config]) -> None:
    """§11.3 — the default (SQLite FTS) path works with zero optional deps installed (I8)."""
    for mod in ("qdrant_client", "fastembed"):
        if importlib.util.find_spec(mod) is not None:
            pytest.skip(f"{mod} present; this env is not base-deps-only")
    config = make_config(backend="sqlite_fts")
    b = get_backend(config)
    try:
        mem_id = b.store("hello world durable memory", {"source": "x"})
        hits = b.search("hello durable memory", k=3, threshold=0.0)
        assert any(h.id == mem_id for h in hits)
    finally:
        b.close()
