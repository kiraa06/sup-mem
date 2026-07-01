"""Backend conformance suite (HANDOVER §11.1) — runs against EVERY backend.

Phase 1 parametrizes only ``sqlite_fts``; Phase 3b appends ``qdrant`` to ``BACKEND_NAMES``
and the exact same assertions must pass there too (I6).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterator

import pytest

from claude_memory.backends import get_backend
from claude_memory.backends.base import MemoryBackend
from claude_memory.config import Config

BACKEND_NAMES = ["sqlite_fts"]

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


@pytest.fixture(params=BACKEND_NAMES)
def backend(
    request: pytest.FixtureRequest, make_config: Callable[..., Config]
) -> Iterator[MemoryBackend]:
    name: str = request.param
    config = make_config(backend=name)
    if name == "qdrant" and importlib.util.find_spec("qdrant_client") is None:
        pytest.skip("qdrant_client not installed")
    b = get_backend(config)
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


def test_reindex_is_safe(backend: MemoryBackend) -> None:
    _seed(backend)
    backend.reindex()  # must not raise
    assert backend.search("postgres pool", k=3, threshold=0.0)  # still searchable


def test_empty_and_nonmatching_queries(backend: MemoryBackend) -> None:
    _seed(backend)
    assert backend.search("", k=5, threshold=0.0) == []
    assert backend.search("!!! ??? ...", k=5, threshold=0.0) == []  # punctuation-only, no terms
    assert backend.search("zzzzzxxxxx qqqqwwwww vvvvbbbb", k=5, threshold=0.0) == []


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
