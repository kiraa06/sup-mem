"""Manifest scale behavior (HANDOVER §11.7, I10)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from claude_memory.backends import get_backend
from claude_memory.config import Config
from claude_memory.manifest import build_manifest


def test_empty_store_manifest_is_blank(make_config: Callable[..., Config]) -> None:
    cfg = make_config(backend="sqlite_fts")
    b = get_backend(cfg)
    try:
        assert build_manifest(b, cfg) == ""
    finally:
        b.close()


def test_small_store_lists_topics_verbatim(make_config: Callable[..., Config]) -> None:
    cfg = make_config(backend="sqlite_fts")  # full_below defaults to 300
    b = get_backend(cfg)
    try:
        for i in range(6):
            b.store(f"a note about area{i}", {"tags": [f"area{i}"], "source": f"s{i}"})
        text = build_manifest(b, cfg)
        assert "summarized" not in text.lower()
        for i in range(6):
            assert f"area{i}" in text
    finally:
        b.close()


@pytest.mark.slow
def test_large_store_summarized_within_budget_and_cached(
    make_config: Callable[..., Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(
        backend="sqlite_fts",
        manifest={"full_below": 50, "token_budget": 60, "max_topics": 100},
    )
    b = get_backend(cfg)
    try:
        for i in range(400):
            b.store(f"memory number {i}", {"tags": [f"tag{i % 80}"], "source": f"s{i}"})

        text = build_manifest(b, cfg)
        assert "summarized" in text.lower()  # count (400) >= full_below (50)
        assert "more" in text  # token budget forced truncation → "(+N more)" tail
        assert len(text) // 4 <= cfg.manifest.token_budget + 25  # within budget (+ header slack)

        # An unchanged store must not re-scan topics on the next build (cache hit, §11.7).
        calls = {"n": 0}
        original = b.manifest

        def counting(max_topics: int) -> list[str]:
            calls["n"] += 1
            return original(max_topics)

        monkeypatch.setattr(b, "manifest", counting)
        assert build_manifest(b, cfg) == text
        assert calls["n"] == 0
    finally:
        b.close()
