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
from sup_mem.ledger import _turn_key as turn_key

SESSION = "sess-1"


def _transcript_entry(
    role: str, text: str, sidechain: bool = False, ts: str = "", **flags: object
) -> str:
    content: object = text if role == "user" else [{"type": "text", "text": text}]
    entry: dict[str, object] = {
        "type": role,
        "isSidechain": sidechain,
        "message": {"role": role, "content": content},
        **flags,
    }
    if ts:
        entry["timestamp"] = ts
    return json.dumps(entry)


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _log_line(
    session: str,
    query: str,
    candidates: list[list[object]],
    ts: str = "2026-07-02T10:00:00+00:00",
) -> str:
    return json.dumps(
        {
            "ts": ts,
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


# --- prompt location + honest attribution (0.9.4) ---------------------------------------
BLUE = "Staging runs a blue-green strategy: deploy.sh flips traffic via Jenkins."
T0 = "2026-07-02T10:00:00+00:00"


def _outcomes(config: Config) -> dict[str, str]:
    with Ledger(config.ledger_db_path) as ledger:
        rows = ledger._conn.execute("SELECT memory_id, outcome FROM candidates").fetchall()
    return {str(r["memory_id"]): str(r["outcome"]) for r in rows}


def test_wrapped_and_block_prompts_are_located(
    config: Config, seeded: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hosts deliver prompts inside isMeta envelopes or as text blocks next to an image; a
    # prefix match on plain strings missed both and fell back to the whole session.
    query = str(seeded["query"])
    for i, user in enumerate(
        [
            _transcript_entry("user", f"<wrapper>\n{query}\n</wrapper>", ts=T0, isMeta=True),
            json.dumps(
                {
                    "type": "user",
                    "timestamp": T0,
                    "message": {
                        "role": "user",
                        "content": [{"type": "image"}, {"type": "text", "text": query}],
                    },
                }
            ),
        ]
    ):
        transcript = tmp_path / f"t{i}.jsonl"
        _write_transcript(transcript, [user, _transcript_entry("assistant", BLUE)])
        config.ledger_db_path.unlink(missing_ok=True)
        _run_stop(config, transcript, monkeypatch)
        out = _outcomes(config)
        assert out[seeded["id_a"]] == "referenced"  # type: ignore[index]
        assert out[seeded["id_b"]] == "ignored"  # type: ignore[index]


def test_absent_prompt_is_unknown_not_whole_session(
    config: Config, seeded: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The prompt never reached the transcript (e.g. a host notification). The assistant DID
    # mention memory A elsewhere in the session — that must not count as using it.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            _transcript_entry("user", "something else entirely"),
            _transcript_entry("assistant", BLUE),
        ],
    )
    _run_stop(config, transcript, monkeypatch)
    assert set(_outcomes(config).values()) == {"unknown", ""}  # "" = the never-injected ghost
    with Ledger(config.ledger_db_path) as ledger:
        assert ledger.stats_for([seeded["id_a"], seeded["id_b"]]) == {}  # type: ignore[list-item]


def test_referenced_means_this_response_not_later_ones(
    config: Config, seeded: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Memory A's tokens show up only in the answer to a LATER prompt → this injection was
    # ignored; it gets credit on the turn where it is actually used.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            _transcript_entry("user", str(seeded["query"])),
            _transcript_entry("assistant", "Let me check the pipeline first."),
            _transcript_entry("user", "and the rollback path?"),
            _transcript_entry("assistant", BLUE),
        ],
    )
    _run_stop(config, transcript, monkeypatch)
    assert _outcomes(config)[seeded["id_a"]] == "ignored"  # type: ignore[index]


def test_repeated_prompt_uses_the_occurrence_nearest_the_log(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = get_backend(config)
    mem = backend.store("The canary rollout uses argo-rollouts with a 10 percent step.", {})
    backend.close()
    config.retrieval_log_path.write_text(
        _log_line(SESSION, "continue", [[mem, 0.9, 1, 15]], ts="2026-07-02T11:00:00+00:00") + "\n",
        encoding="utf-8",
    )
    transcript = tmp_path / "t.jsonl"
    canary = "Using argo-rollouts: the canary advances in a 10 percent step."
    _write_transcript(
        transcript,
        [
            _transcript_entry("user", "continue", ts="2026-07-02T10:00:00Z"),
            _transcript_entry("assistant", canary),  # an older turn's answer
            _transcript_entry("user", "continue", ts="2026-07-02T11:00:01Z"),
            _transcript_entry("assistant", "Done — nothing else to change."),
        ],
    )
    _run_stop(config, transcript, monkeypatch)
    assert _outcomes(config)[mem] == "ignored"  # judged against 11:00's answer, not 10:00's

    # Only a far-off occurrence exists → it is another turn → unknown, never a guess.
    config.ledger_db_path.unlink()
    _write_transcript(transcript, [_transcript_entry("user", "continue", ts=T0)])
    _run_stop(config, transcript, monkeypatch)
    assert _outcomes(config)[mem] == "unknown"


def test_contradiction_ignores_summaries_and_mid_sentence_words(
    config: Config, seeded: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript: Path = seeded["transcript"]  # type: ignore[assignment]
    _run_stop(config, transcript, monkeypatch)
    with transcript.open("a", encoding="utf-8") as fh:
        # A compaction summary is not the user reacting, even if it says "No," somewhere.
        fh.write(
            _transcript_entry("user", "No, this session is being continued.", isCompactSummary=True)
            + "\n"
        )
        fh.write(_transcript_entry("user", "great — I actually want the rollback steps too") + "\n")
    _run_stop(config, transcript, monkeypatch)
    assert _outcomes(config)[seeded["id_a"]] == "referenced"  # type: ignore[index]


def test_rows_after_log_rotation_are_not_dropped(
    config: Config, seeded: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript: Path = seeded["transcript"]  # type: ignore[assignment]
    _run_stop(config, transcript, monkeypatch)
    # Rotation rewrites the log; a NEW turn now sits at line 0 again — the old key space.
    later = "2026-07-02T12:00:00+00:00"
    config.retrieval_log_path.write_text(
        _log_line(SESSION, "later question", [[seeded["id_b"], 0.7, 1, 18]], ts=later) + "\n",
        encoding="utf-8",
    )
    with Ledger(config.ledger_db_path) as ledger:
        ledger._conn.execute("UPDATE cursors SET log_line = 0")
        ledger._conn.commit()
        assert ledger.ingest_log(SESSION, config.retrieval_log_path) == 1
        keys = {r[0] for r in ledger._conn.execute("SELECT line_no FROM candidates")}
    assert turn_key(later, 0) in keys and turn_key(T0, 0) in keys


def test_repair_backs_out_unlocated_attributions_once(config: Config) -> None:
    with Ledger(config.ledger_db_path) as ledger:
        conn = ledger._conn
        conn.executemany(
            "INSERT INTO candidates (session_id, line_no, memory_id, injected, tokens, outcome, "
            "prompt_turn) VALUES ('s', ?, ?, 1, 10, ?, ?)",
            [(1, "m", "referenced", -1), (2, "m", "contradicted", -1), (3, "m", "referenced", 4)],
        )
        conn.execute(
            "INSERT INTO stats (memory_id, injected, referenced, contradicted, tokens) "
            "VALUES ('m', 3, 2, 1, 30)"
        )
        conn.commit()
        assert ledger.repair_unlocated() == 2
        assert ledger.repair_unlocated() == 0  # one-time
        m = ledger.stats_for(["m"])["m"]
    assert (m["injected"], m["referenced"], m["contradicted"], m["tokens"]) == (1, 1, 0, 10)


def test_legacy_default_correction_patterns_are_upgraded(make_config: object) -> None:
    from sup_mem.config import CORRECTION_PATTERNS, LEGACY_CORRECTION_PATTERNS, load_config

    cfg = make_config()  # type: ignore[operator]
    lines = "\n".join(f"    '{p}'," for p in LEGACY_CORRECTION_PATTERNS)
    cfg.config_path.write_text(f"[ledger]\ncorrection_patterns = [\n{lines}\n]\n")
    upgraded = load_config(overrides={"data_dir": str(cfg.data_dir)})
    assert upgraded.ledger.correction_patterns == list(CORRECTION_PATTERNS)
    cfg.config_path.write_text("[ledger]\ncorrection_patterns = ['^custom']\n")
    custom = load_config(overrides={"data_dir": str(cfg.data_dir)})
    assert custom.ledger.correction_patterns == ["^custom"]  # a user's own list is respected
