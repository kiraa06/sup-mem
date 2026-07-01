"""Hook tier logic + hot-path guarantees (HANDOVER §11.2, §11.4, §11.6)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_memory.backends import get_backend
from claude_memory.backends.base import MemoryBackend
from claude_memory.config import Config
from claude_memory.hook import user_prompt_submit as hook
from claude_memory.models import Hit


def _run_main(
    prompt: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
) -> tuple[int, str]:
    monkeypatch.setenv("CLAUDE_MEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CLAUDE_MEMORY_LOGGING_RETRIEVAL_LOG", "false")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": prompt})))
    rc = hook.main()
    return rc, capsys.readouterr().out


# --- Tier 1 skip-gate logic (§11.4) ------------------------------------------------------
def test_should_skip_greeting_and_thanks(config: Config) -> None:
    assert hook._should_skip("thanks!", config) is True
    assert hook._should_skip("hey", config) is True
    assert hook._should_skip("ok cool", config) is True


def test_cue_overrides_skip(config: Config) -> None:
    # Trivial opener but references prior work → must NOT skip (I3: skip iff skip-match AND no cue).
    assert hook._should_skip("thanks for the fix we did", config) is False
    assert hook._should_skip("ok, and DEVOPS-1234?", config) is False


def test_substantive_prompt_not_skipped(config: Config) -> None:
    assert hook._should_skip("How should I structure the retry logic here?", config) is False


# --- Tier behavior end to end ------------------------------------------------------------
def test_skip_injects_only_tier0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    (data_dir / "pinned.md").write_text("User prefers concise answers.", encoding="utf-8")
    rc, out = _run_main("thanks!", monkeypatch, capsys, data_dir)
    assert rc == 0
    assert "Pinned facts" in out  # Tier 0 always injected
    assert "auto-retrieved" not in out  # Tier 2 header absent on a skipped turn


def test_retrieval_injects_relevant_memory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], config: Config
) -> None:
    b = get_backend(config)
    b.store(
        "The database schema migration to Postgres 15 shipped in June.",
        {"source": "s1", "tags": ["postgres", "migration"]},
    )
    b.close()
    rc, out = _run_main(
        "remind me about the database schema migration to postgres",
        monkeypatch,
        capsys,
        config.data_dir,
    )
    assert rc == 0
    assert "Postgres 15" in out
    assert "auto-retrieved" in out


def _cfg(data_dir: Path) -> Config:
    from claude_memory.config import load_config

    return load_config(overrides={"data_dir": str(data_dir)})


def test_below_threshold_hits_are_dropped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    # A weak match exists ("database") but a near-1 threshold must drop it (§11.4 Tier-2).
    b = get_backend(_cfg(data_dir))
    b.store("The database backups run nightly to the S3 archive bucket.", {"source": "s1"})
    b.close()
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_THRESHOLD", "0.999")
    rc, out = _run_main("database performance tuning strategies", monkeypatch, capsys, data_dir)
    assert rc == 0
    assert "auto-retrieved" not in out


def test_fail_open_when_backend_search_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    class Boom(MemoryBackend):
        def store(self, text: str, metadata: object = None) -> str:  # pragma: no cover
            return "x"

        def search(self, query: str, k: int, threshold: float) -> list[Hit]:
            raise RuntimeError("backend down")

        def manifest(self, max_topics: int) -> list[str]:  # pragma: no cover
            return []

        def health(self) -> dict[str, object]:  # pragma: no cover
            return {}

        def reindex(self, progress: object = None) -> None:  # pragma: no cover
            return None

    monkeypatch.setattr("claude_memory.backends.get_backend", lambda _c: Boom())
    rc, out = _run_main("what did we decide about the schema?", monkeypatch, capsys, data_dir)
    assert rc == 0
    assert out.strip() == ""  # no pinned + retrieval failed silently → nothing injected


# --- Lazy import on the skip path (§11.2, build-breaking if it regresses) -----------------
def test_skip_path_imports_nothing_heavy(tmp_path: Path) -> None:
    script = (
        "import io, json, sys\n"
        'sys.stdin = io.StringIO(json.dumps({"prompt": "thanks!"}))\n'
        "import claude_memory.hook.user_prompt_submit as h\n"
        "h.main()\n"
        "heavy = [m for m in ("
        "'claude_memory.backends', 'claude_memory.backends.sqlite_fts',"
        " 'sqlite3', 'fastembed', 'qdrant_client') if m in sys.modules]\n"
        "sys.stderr.write('HEAVY=' + ','.join(heavy))\n"
        "sys.exit(2 if heavy else 0)\n"
    )
    env = {
        **os.environ,
        "CLAUDE_MEMORY_DATA_DIR": str(tmp_path),
        "CLAUDE_MEMORY_LOGGING_RETRIEVAL_LOG": "false",
    }
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"heavy modules imported on skip path -> {proc.stderr}"
