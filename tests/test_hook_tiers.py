"""Hook tier logic + hot-path guarantees (HANDOVER §11.2, §11.4, §11.6)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sup_mem.backends import get_backend
from sup_mem.backends.base import MemoryBackend
from sup_mem.config import Config
from sup_mem.hook import user_prompt_submit as hook
from sup_mem.models import Hit


def _run_main(
    prompt: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    data_dir: Path,
) -> tuple[int, str]:
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SUP_MEM_LOGGING_RETRIEVAL_LOG", "false")
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
    from sup_mem.config import load_config

    return load_config(overrides={"data_dir": str(data_dir)})


def test_below_threshold_hits_are_dropped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    # A weak match exists ("database") but a near-1 threshold must drop it (§11.4 Tier-2).
    b = get_backend(_cfg(data_dir))
    b.store("The database backups run nightly to the S3 archive bucket.", {"source": "s1"})
    b.close()
    monkeypatch.setenv("SUP_MEM_RETRIEVAL_THRESHOLD", "0.999")
    rc, out = _run_main("database performance tuning strategies", monkeypatch, capsys, data_dir)
    assert rc == 0
    assert "auto-retrieved" not in out


def test_fail_open_when_backend_search_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    class Boom(MemoryBackend):
        def store(self, text: str, metadata: object = None) -> str:  # pragma: no cover
            return "x"

        def search(
            self, query: str, k: int, threshold: float, as_of: str | None = None
        ) -> list[Hit]:
            raise RuntimeError("backend down")

        def manifest(self, max_topics: int) -> list[str]:  # pragma: no cover
            return []

        def health(self) -> dict[str, object]:  # pragma: no cover
            return {}

        def reindex(self, progress: object = None) -> None:  # pragma: no cover
            return None

        def fetch(self, memory_ids: list[str]) -> dict[str, str]:  # pragma: no cover
            return {}

    monkeypatch.setattr("sup_mem.backends.get_backend", lambda _c: Boom())
    rc, out = _run_main("what did we decide about the schema?", monkeypatch, capsys, data_dir)
    assert rc == 0
    assert out.strip() == ""  # no pinned + retrieval failed silently → nothing injected


# --- Injection clipping (max_inject_chars) -------------------------------------------------
def test_long_memory_is_clipped_with_recall_pointer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    b = get_backend(_cfg(data_dir))
    b.store(
        "The renewal pipeline for certificates rotates every node sequentially. " * 40,
        {"source": "s1"},
    )
    b.close()
    monkeypatch.setenv("SUP_MEM_RETRIEVAL_MAX_INJECT_CHARS", "200")
    rc, out = _run_main(
        "how does the certificate renewal pipeline rotate nodes?", monkeypatch, capsys, data_dir
    )
    assert rc == 0
    assert "truncated — use recall" in out
    memory_line = next(line for line in out.splitlines() if "renewal pipeline" in line)
    assert len(memory_line) < 350  # 200-char clip + marker + score, nowhere near the full text


def test_clip_zero_means_unlimited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    long_text = "The renewal pipeline for certificates rotates nodes. " * 30
    b = get_backend(_cfg(data_dir))
    b.store(long_text, {"source": "s1"})
    b.close()
    monkeypatch.setenv("SUP_MEM_RETRIEVAL_MAX_INJECT_CHARS", "0")
    rc, out = _run_main(
        "how does the certificate renewal pipeline work?", monkeypatch, capsys, data_dir
    )
    assert rc == 0
    assert "truncated" not in out
    assert long_text.strip() in out


def test_logged_token_estimate_reflects_clip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data_dir: Path
) -> None:
    import json as _json

    b = get_backend(_cfg(data_dir))
    b.store("certificate renewal rotates nodes " * 100, {"source": "s1"})  # ~3400 chars
    b.close()
    # _run_main disables the retrieval log; this test needs it on, so drive main() directly.
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SUP_MEM_RETRIEVAL_MAX_INJECT_CHARS", "400")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"prompt": "certificate renewal nodes rotation"}))
    )
    assert hook.main() == 0
    capsys.readouterr()
    entry = _json.loads((data_dir / "retrieval.jsonl").read_text().splitlines()[-1])
    tokens = [c[3] for c in entry["candidates"]]
    assert tokens and all(t <= 100 for t in tokens)  # 400 chars // 4, not the ~850 full estimate


# --- Lazy import on the skip path (§11.2, build-breaking if it regresses) -----------------
def test_skip_path_imports_nothing_heavy(tmp_path: Path) -> None:
    script = (
        "import io, json, sys\n"
        'sys.stdin = io.StringIO(json.dumps({"prompt": "thanks!"}))\n'
        "import sup_mem.hook.user_prompt_submit as h\n"
        "h.main()\n"
        "heavy = [m for m in ("
        "'sup_mem.backends', 'sup_mem.backends.sqlite_fts',"
        " 'sqlite3', 'fastembed', 'qdrant_client') if m in sys.modules]\n"
        "sys.stderr.write('HEAVY=' + ','.join(heavy))\n"
        "sys.exit(2 if heavy else 0)\n"
    )
    env = {
        **os.environ,
        "SUP_MEM_DATA_DIR": str(tmp_path),
        "SUP_MEM_LOGGING_RETRIEVAL_LOG": "false",
    }
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"heavy modules imported on skip path -> {proc.stderr}"
