"""Embedding auto-detection (HANDOVER §11.8). Provider availability is fully mocked."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sup_mem.config import Config
from sup_mem.embedding import detect
from sup_mem.embedding.base import EmbeddingError, EmbeddingMeta, provider_is_hook_safe

# Every probe reports (available, model). Patch them to simulate an environment.
_PROBES = {
    "available_ollama": "ollama",
    "available_fastembed": "fastembed",
    "available_tei": "tei",
    "available_voyage": "voyage",
    "available_openai": "openai",
}


def _set_availability(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> None:
    from sup_mem.embedding import providers

    for attr, name in _PROBES.items():
        model = providers.SPEC_BY_NAME[name].default_model if name in available else ""
        monkeypatch.setattr(providers, attr, lambda _c, m=model: (bool(m), m))


def _quiet(_msg: str) -> None:
    pass


def test_priority_prefers_ollama_over_others(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    _set_availability(monkeypatch, {"ollama", "fastembed", "openai"})
    sel = detect.detect_embedding_provider(config, assume_yes=True, log=_quiet)
    assert sel.provider == "ollama"


def test_falls_back_to_fastembed_when_no_server(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    _set_availability(monkeypatch, {"fastembed", "voyage"})
    sel = detect.detect_embedding_provider(config, assume_yes=True, log=_quiet)
    assert sel.provider == "fastembed"
    assert sel.model == "BAAI/bge-small-en-v1.5"
    assert sel.dim == 384  # recorded (provider, model, dim), read back below


def test_non_interactive_picks_top_available(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    # Only hosted providers available → openai wins over voyage by priority order.
    _set_availability(monkeypatch, {"voyage", "openai"})
    sel = detect.detect_embedding_provider(config, interactive=False, log=_quiet)
    assert sel.provider == "voyage"  # voyage precedes openai in PROVIDER_SPECS


def test_nothing_available_raises_with_remediation(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    _set_availability(monkeypatch, set())
    with pytest.raises(EmbeddingError) as excinfo:
        detect.detect_embedding_provider(config, assume_yes=True, log=_quiet)
    message = str(excinfo.value)
    assert "install" in message.lower()
    assert "ollama" in message.lower()


def test_pinned_provider_is_respected(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., Config]
) -> None:
    cfg = make_config(embedding={"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"})
    # Even with ollama available, the pinned provider is used (I7 / respect the user).
    _set_availability(monkeypatch, {"ollama", "fastembed"})
    sel = detect.detect_embedding_provider(cfg, assume_yes=True, log=_quiet)
    assert sel.provider == "fastembed"


def test_pinned_but_unreachable_raises(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., Config]
) -> None:
    cfg = make_config(embedding={"provider": "ollama"})
    _set_availability(monkeypatch, set())  # ollama not reachable
    with pytest.raises(EmbeddingError):
        detect.detect_embedding_provider(cfg, assume_yes=True, log=_quiet)


def test_hook_safety_classification() -> None:
    # I2: remote embedders are hook-safe; in-process fastembed is not.
    assert provider_is_hook_safe("ollama") is True
    assert provider_is_hook_safe("openai") is True
    assert provider_is_hook_safe("fastembed") is False


def test_embedding_meta_roundtrip() -> None:
    meta = EmbeddingMeta("fastembed", "BAAI/bge-small-en-v1.5", 384)
    assert EmbeddingMeta.from_dict(meta.as_dict()) == meta
    assert meta.mismatch(EmbeddingMeta("fastembed", "other-model", 384)) is True
    assert meta.mismatch(EmbeddingMeta("fastembed", "BAAI/bge-small-en-v1.5", 384)) is False
