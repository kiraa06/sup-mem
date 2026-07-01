"""Shared pytest fixtures.

Every fixture points ``data_dir`` at a tmp path so tests never read or write the developer's
real ``~/.claude-memory`` and never pick up ambient config/env.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_memory.config import ENV_PREFIX, Config, load_config


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any CLAUDE_MEMORY_* env vars so the developer's shell can't skew a test run."""
    for key in list(__import__("os").environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "claude-memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def config(data_dir: Path) -> Config:
    return load_config(overrides={"data_dir": str(data_dir)})


@pytest.fixture
def make_config(data_dir: Path) -> Iterator[object]:
    """Factory returning a Config with ad-hoc overrides layered on the tmp data dir."""

    def _make(**overrides: object) -> Config:
        merged: dict[str, object] = {"data_dir": str(data_dir), **overrides}
        return load_config(overrides=merged)

    yield _make
