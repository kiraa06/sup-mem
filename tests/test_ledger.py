"""The outcome ledger: attribution, cursors, sidechains, fail-open (PHASE6 acceptance 1/7)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from sup_mem.backends import get_backend
from sup_mem.config import Config
from sup_mem.hook import stop as stop_hook
from sup_mem.ledger import Ledger, parse_transcript

SESSION = "sess-1"


def _transcript_entry(role: str, text: str, sidechain: bool = False) -> str:
    content: object = text if role == "user" else [{"type": "text", "text": text}]
    return json.dumps(
        {"type": role, "isSidechain": sidechain, "message": {"role": role, "content": content}}
    )


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _log_line(session: str, query: str, candidates: list[list[object]]) -> str:
    return json.dumps(
        {
            "ts": "2026-07-02T10:00:00+00:00",
            "session_id": session,
            "query": query,
            "tier": "retrieve",
            "injected_ids": [c[0] for c in candidates if c[2]],
            "scores": [c[1] for c in candidates if c[2]],
            "candidates": candidates,
        }
    )


@pytest.fixture
def seeded(config: Config, tmp_path: Path) -> dict[str, object]:
    """A store with two memories, a retrieval log injecting both, and a transcript where the
    assistant clearly uses memory A (blue-green tokens) and never touches memory B."""
    backend = get_backend(config)
    id_a = backend.store(
        "The staging deploy uses a blue-green strategy driven by deploy.sh in Jenkins.",
        {"source": "a"},
    )
    id_b = backend.store(
        "The Postgres connection pool maximum was raised to fifty for timeout mitigation.",
        {"source": "b"},
    )
    backend.close()

    query = "how does the staging deploy work exactly?"
    config.retrieval_log_path.write_text(
        _log_line(SESSION, query, [[id_a, 0.8, 1, 20], [id_b, 0.5, 1, 18], ["ghost", 0.2, 0, 9]])
        + "\n",
        encoding="utf-8",
    )

    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(
        transcript,
        [
            _transcript_entry("user", query),
            _transcript_entry(
                "assistant",
                "Staging runs a blue-green strategy: deploy.sh flips traffic via Jenkins.",
            ),
        ],
    )
    return {"id_a": id_a, "id_b": id_b, "transcript": transcript, "query": query}


def _run_stop(config: Config, transcript: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(config.data_dir))
    payload = json.dumps({"session_id": SESSION, "transcript_path": str(transcript)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    return stop_hook.main()


def test_attribution_referenced_vs_ignored(
    config: Config, seeded: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run_stop(config, seeded["transcript"], monkeypatch) == 0  # type: ignore[arg-type]
    with Ledger(config.ledger_db_path) as ledger:
        stats = ledger.stats_for([seeded["id_a"], seeded["id_b"]])  # type: ignore[list-item]
    a, b = stats[seeded["id_a"]], stats[seeded["id_b"]]  # type: ignore[index]
    assert a["referenced"] == 1 and a["ignored"] == 0
    assert b["referenced"] == 0 and b["ignored"] == 1
    assert a["injected"] == 1 and b["injected"] == 1
    assert a["tokens"] == 20  # injected token estimate accumulated


def test_second_run_adds_nothing(
    config: Config, seeded: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_stop(config, seeded["transcript"], monkeypatch)  # type: ignore[arg-type]
    _run_stop(config, seeded["transcript"], monkeypatch)  # type: ignore[arg-type]
    with Ledger(config.ledger_db_path) as ledger:
        stats = ledger.stats_for([seeded["id_a"]])  # type: ignore[list-item]
    assert stats[seeded["id_a"]]["injected"] == 1  # type: ignore[index]  # cursor dedupes


def test_contradiction_flips_on_later_stop(
    config: Config, seeded: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript: Path = seeded["transcript"]  # type: ignore[assignment]
    _run_stop(config, transcript, monkeypatch)
    # The user pushes back in the next turn; a later Stop re-checks referenced rows.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(_transcript_entry("user", "no, that is outdated — we moved off deploy.sh") + "\n")
    _run_stop(config, transcript, monkeypatch)
    with Ledger(config.ledger_db_path) as ledger:
        stats = ledger.stats_for([seeded["id_a"]])  # type: ignore[list-item]
    a = stats[seeded["id_a"]]  # type: ignore[index]
    assert a["contradicted"] == 1 and a["referenced"] == 0


def test_sidechain_text_does_not_count(
    config: Config, seeded: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Distinctive tokens appear ONLY inside a sidechain (subagent) entry → still ignored.
    transcript = tmp_path / "side.jsonl"
    _write_transcript(
        transcript,
        [
            _transcript_entry("user", str(seeded["query"])),
            _transcript_entry(
                "assistant",
                "Staging runs a blue-green strategy: deploy.sh flips traffic via Jenkins.",
                sidechain=True,
            ),
            _transcript_entry("assistant", "Let me look into that for you."),
        ],
    )
    _run_stop(config, transcript, monkeypatch)
    with Ledger(config.ledger_db_path) as ledger:
        stats = ledger.stats_for([seeded["id_a"]])  # type: ignore[list-item]
    assert stats[seeded["id_a"]]["ignored"] == 1  # type: ignore[index]


def test_stop_hook_fails_open(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(config.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO("this is not json {{{"))
    assert stop_hook.main() == 0
    payload = json.dumps({"session_id": "s", "transcript_path": "/nope/missing.jsonl"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert stop_hook.main() == 0


def test_parse_transcript_skips_tool_results_and_garbage(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    tool_result_user = json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result"}]}}
    )
    _write_transcript(
        path,
        [
            "not json at all",
            _transcript_entry("user", "real prompt"),
            tool_result_user,
            _transcript_entry("assistant", "real answer"),
        ],
    )
    turns = parse_transcript(path)
    assert [(t.role, t.text) for t in turns] == [
        ("user", "real prompt"),
        ("assistant", "real answer"),
    ]
