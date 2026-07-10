"""Outcome-reinforced ranking + tune + roi (PHASE6 acceptance 2/3/4)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from sup_mem import commands
from sup_mem.config import Config
from sup_mem.models import Hit
from sup_mem.ranking import adjust


def _seed_stats(db: Path, rows: list[tuple[str, int, int, int, int]]) -> None:
    """(memory_id, injected, referenced, ignored, contradicted) — creates schema via Ledger."""
    from sup_mem.ledger import Ledger

    Ledger(db).close()  # ensure schema
    conn = sqlite3.connect(str(db))
    with conn:
        for mem_id, injected, referenced, ignored, contradicted in rows:
            conn.execute(
                "INSERT INTO stats (memory_id, injected, referenced, ignored, contradicted) "
                "VALUES (?, ?, ?, ?, ?)",
                (mem_id, injected, referenced, ignored, contradicted),
            )
    conn.close()


def _seed_candidates(db: Path, turns: list[list[tuple[str, float, int, str, int]]]) -> None:
    """Each turn: [(memory_id, score, injected, outcome, tokens)] on its own line_no."""
    from sup_mem.ledger import Ledger

    Ledger(db).close()
    conn = sqlite3.connect(str(db))
    with conn:
        for line_no, turn in enumerate(turns):
            for mem_id, score, injected, outcome, tokens in turn:
                conn.execute(
                    "INSERT INTO candidates "
                    "(session_id, line_no, memory_id, score, injected, outcome, tokens) "
                    "VALUES ('s', ?, ?, ?, ?, ?, ?)",
                    (line_no, mem_id, score, injected, outcome, tokens),
                )
    conn.close()


# --- ranking (acceptance 2) ----------------------------------------------------------------
def test_referenced_outranks_ignored_at_equal_base(config: Config) -> None:
    _seed_stats(config.ledger_db_path, [("good", 3, 3, 0, 0), ("noise", 3, 0, 3, 0)])
    hits = [Hit("noise", "n", 0.50), Hit("good", "g", 0.50)]
    out = adjust(hits, config)
    assert [h.id for h in out] == ["good", "noise"]
    assert out[0].score > 0.50 and all(0.0 <= h.score <= 1.0 for h in out)
    assert out[1].score == 0.50  # ignored gets no negative boost — only contradictions demote


def test_quarantined_memory_is_dropped(config: Config) -> None:
    _seed_stats(config.ledger_db_path, [("bad", 4, 0, 1, 3)])
    out = adjust([Hit("bad", "b", 0.9), Hit("fresh", "f", 0.4)], config)
    assert [h.id for h in out] == ["fresh"]


def test_chronic_ignore_is_demoted_below_a_fresh_hit(config: Config) -> None:
    # "stale": injected 10x, never referenced (no contradictions, so NOT quarantined) → the
    # chronic-ignore penalty (-0.20) down-ranks it below an untouched, lower-scored fresh hit.
    _seed_stats(config.ledger_db_path, [("stale", 10, 0, 10, 0)])
    out = adjust([Hit("stale", "s", 0.85), Hit("fresh", "f", 0.70)], config)
    assert next(h for h in out if h.id == "stale").score == pytest.approx(0.65)
    assert [h.id for h in out] == ["fresh", "stale"]  # demoted, not dropped


def test_chronic_ignore_gate_spares_new_and_used_memories(config: Config) -> None:
    # "new": ignored but only injected 3x (< min 8) → too little evidence → NOT penalized.
    # "used": injected 10x with 5 references (50% >= 10%) → earns its keep → NOT penalized.
    _seed_stats(config.ledger_db_path, [("new", 3, 0, 3, 0), ("used", 10, 5, 5, 0)])
    out = adjust([Hit("new", "n", 0.85), Hit("used", "u", 0.85)], config)
    scores = {h.id: h.score for h in out}
    assert scores["new"] == pytest.approx(0.85)  # spared: below the min-injections gate
    assert scores["used"] >= 0.85  # spared (and boosted for being referenced)


def test_disabled_ledger_is_passthrough(make_config: Callable[..., Config]) -> None:
    cfg = make_config(ledger={"enabled": False})
    _seed_stats(cfg.ledger_db_path, [("good", 3, 3, 0, 0)])
    hits = [Hit("x", "x", 0.4), Hit("good", "g", 0.3)]
    assert adjust(hits, cfg) == hits


def test_missing_ledger_fails_open(config: Config) -> None:
    hits = [Hit("a", "a", 0.5)]
    assert adjust(hits, config) == hits  # no ledger.db on disk → unchanged


# --- tune (acceptance 3) -------------------------------------------------------------------
def test_tune_recommends_highest_lossless_threshold(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_candidates(
        config.ledger_db_path,
        [
            [
                ("a", 0.80, 1, "referenced", 30),
                ("b", 0.40, 1, "ignored", 25),
                ("c", 0.20, 0, "", 10),
            ],
            [("a", 0.75, 1, "referenced", 30), ("d", 0.45, 1, "ignored", 40)],
        ],
    )
    assert commands.cmd_tune(config) == 0
    out = capsys.readouterr().out
    assert "0.75" in out and "★rec" in out  # highest θ keeping both referenced injections
    assert "unknown" in out.lower()  # L4 honesty is surfaced


def test_tune_apply_writes_config(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_candidates(config.ledger_db_path, [[("a", 0.80, 1, "referenced", 30)]])
    assert commands.cmd_tune(config, apply=True) == 0
    text = config.config_path.read_text(encoding="utf-8")
    assert "threshold = 0.8" in text

    from sup_mem.config import load_config

    reloaded = load_config(overrides={"data_dir": str(config.data_dir)})
    assert reloaded.retrieval.threshold == 0.8


def test_tune_without_data_is_friendly(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert commands.cmd_tune(config) == 0
    assert "not enough outcome data" in capsys.readouterr().out.lower()


# --- k replay ---------------------------------------------------------------------------------
def test_replay_k_counts_and_recommendation() -> None:
    from sup_mem.ledger import recommend_k, replay_k

    turns = [
        [  # score-DESC, as candidate_turns() returns them
            {"memory_id": "top", "score": 0.90, "injected": 1, "outcome": "ignored", "tokens": 30},
            {
                "memory_id": "snd",
                "score": 0.85,
                "injected": 1,
                "outcome": "referenced",
                "tokens": 30,
            },
            {"memory_id": "pool", "score": 0.50, "injected": 0, "outcome": "", "tokens": 10},
        ]
    ]
    rows = replay_k(turns, 0.35, 2)
    by_k = {int(r["k"]): r for r in rows}
    assert set(by_k) == {1, 2, 3}  # 1..current_k+1
    assert by_k[1]["lost_ref"] == 1  # the rank-2 referenced injection is lost at k=1
    assert by_k[2]["lost_ref"] == 0 and by_k[2]["cut_ign"] == 0
    assert by_k[3]["unknown"] == 1  # k above current only adds never-injected outcomes (L4)
    assert recommend_k(rows, 2) == 2  # k=1 is lossy; k=3 is never recommended (raising = guess)

    lossless = [
        [
            {
                "memory_id": "top",
                "score": 0.90,
                "injected": 1,
                "outcome": "referenced",
                "tokens": 30,
            },
            {"memory_id": "snd", "score": 0.85, "injected": 1, "outcome": "ignored", "tokens": 30},
        ]
    ]
    assert recommend_k(replay_k(lossless, 0.35, 2), 2) == 1  # every ref at rank 1 → shrink


def test_tune_apply_writes_both_theta_and_k(
    config: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    # Single injected+referenced candidate at rank 1 → θ can rise to 0.8 AND k can shrink to 1,
    # both losslessly; --apply must land both in ONE config write.
    _seed_candidates(
        config.ledger_db_path,
        [[("a", 0.80, 1, "referenced", 30), ("b", 0.40, 1, "ignored", 25)]],
    )
    assert commands.cmd_tune(config, apply=True) == 0
    out = capsys.readouterr().out
    assert "Recommended k:" in out and "★rec" in out

    text = config.config_path.read_text(encoding="utf-8")
    assert "threshold = 0.8" in text
    assert "\nk = 1" in text  # line-anchored: must not match pool_k

    from sup_mem.config import load_config

    reloaded = load_config(overrides={"data_dir": str(config.data_dir)})
    assert reloaded.retrieval.threshold == 0.8 and reloaded.retrieval.k == 1


# --- roi (acceptance 4) ----------------------------------------------------------------------
def test_roi_totals_match_ledger(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    from sup_mem.backends import get_backend

    backend = get_backend(config)
    mem_id = backend.store("the canonical deploy pipeline notes", {"source": "s"})
    backend.close()
    _seed_stats(config.ledger_db_path, [(mem_id, 5, 2, 3, 0), ("gone", 4, 0, 4, 0)])

    conn = sqlite3.connect(str(config.ledger_db_path))
    with conn:
        conn.execute("UPDATE stats SET tokens = 120 WHERE memory_id = ?", (mem_id,))
    conn.close()

    assert commands.cmd_roi(config) == 0
    out = capsys.readouterr().out
    assert "9 injections" in out  # 5 + 4
    assert "2 referenced" in out
    assert "valuable" in out and "wasteful" in out


def test_roi_top_limits_rows_but_totals_span_all(
    config: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sup_mem.backends import get_backend

    monkeypatch.setenv("COLUMNS", "200")  # keep rich from wrapping the title/totals in capture
    backend = get_backend(config)
    ids = [backend.store(f"memory {i} on deploy pipelines", {"source": f"s{i}"}) for i in range(6)]
    backend.close()
    _seed_stats(config.ledger_db_path, [(mid, 5, 1, 4, 0) for mid in ids])  # 6 memories, 30 inj

    assert commands.cmd_roi(config, top=2) == 0
    out = capsys.readouterr().out
    assert "top 2 of 6" in out  # table truncated to the top spenders
    assert "4 more" in out  # hidden-row hint
    assert "6 memories" in out and "30 injections" in out  # totals still span every memory


def test_roi_without_data_is_friendly(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert commands.cmd_roi(config) == 0
    assert "no outcome data" in capsys.readouterr().out.lower()
