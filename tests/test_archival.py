"""Size-bounded decay archival (PHASE9 acceptance 1–6)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from sup_mem import commands
from sup_mem.archival import run_archival
from sup_mem.backends import get_backend
from sup_mem.backends.sqlite_fts import SqliteFtsBackend
from sup_mem.config import Config

SRC = "native:proj/fact.md"


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _shift_recorded(config: Config, mem_id: str, days_ago: int) -> None:
    conn = sqlite3.connect(str(config.db_path))
    with conn:
        conn.execute("UPDATE memories SET recorded_at = ? WHERE id = ?", (_iso(days_ago), mem_id))
    conn.close()


def _shift_superseded(config: Config, mem_id: str, days_ago: int) -> None:
    conn = sqlite3.connect(str(config.db_path))
    with conn:
        conn.execute("UPDATE memories SET superseded_at = ? WHERE id = ?", (_iso(days_ago), mem_id))
    conn.close()


def _seed_stats(config: Config, rows: list[tuple[str, int, int, int, int]]) -> None:
    from sup_mem.ledger import Ledger

    Ledger(config.ledger_db_path).close()
    conn = sqlite3.connect(str(config.ledger_db_path))
    with conn:
        for mem_id, injected, referenced, ignored, contradicted in rows:
            conn.execute(
                "INSERT OR REPLACE INTO stats "
                "(memory_id, injected, referenced, ignored, contradicted) "
                "VALUES (?, ?, ?, ?, ?)",
                (mem_id, injected, referenced, ignored, contradicted),
            )
    conn.close()


@pytest.fixture
def aged_supersession(config: Config) -> dict[str, str]:
    """old version superseded 120d ago (→ steady tier) + a live successor."""
    backend = get_backend(config)
    old = backend.store("old belief about pod restarts being OOM", {"source": SRC})
    new = backend.store("new belief: BouncyCastle duplicate jars", {"source": SRC})
    backend.close()
    _shift_recorded(config, old, 150)
    _shift_superseded(config, old, 120)
    return {"old": old, "new": new}


# --- regime 1: steady state -------------------------------------------------------------
def test_steady_state_archives_old_superseded(
    config: Config, aged_supersession: dict[str, str]
) -> None:
    report = run_archival(config)
    assert report["steady"] == [aged_supersession["old"]]
    assert report["purged"] == []

    backend = get_backend(config)
    try:
        # off the hot store, still first-class for as-of + fetch + verify (A3/A4)
        assert backend.health()["versions"] == 1
        texts = backend.fetch([aged_supersession["old"]])
        assert "OOM" in texts[aged_supersession["old"]]
        hits = backend.search("pod restarts OOM belief", k=5, threshold=0.0, as_of=_iso(130))
        assert [h.id for h in hits] == [aged_supersession["old"]]
        assert backend.verify_provenance()["ok"]
    finally:
        backend.close()


def test_recent_supersession_stays_hot(config: Config) -> None:
    backend = get_backend(config)
    old = backend.store("v1 of fact", {"source": SRC})
    backend.store("v2 of fact", {"source": SRC})  # superseded just now
    backend.close()
    report = run_archival(config)
    assert old not in report["steady"]


def test_restore_roundtrip(config: Config, aged_supersession: dict[str, str]) -> None:
    run_archival(config)
    assert commands.cmd_restore(config, [aged_supersession["old"]]) == 0
    backend = get_backend(config)
    try:
        assert backend.health()["versions"] == 2  # back in the hot store
        hits = backend.search("pod restarts", k=5, threshold=0.0, as_of=_iso(130))
        assert [h.id for h in hits] == [aged_supersession["old"]]  # state intact (superseded)
        assert backend.verify_provenance()["ok"]  # archived+restored chain events consistent
    finally:
        backend.close()


# --- regime 2: main pressure ------------------------------------------------------------
def _bulk_store(
    config: Config, n: int, label: str, tag: str | None = None, days_ago: int = 60
) -> list[str]:
    backend = get_backend(config)
    ids = []
    for i in range(n):
        # Distinct source per row and per group — same-source stores would supersede (T3).
        meta: dict[str, object] = {"source": f"session:bulk-{label}-{i}"}
        if tag:
            meta["tags"] = [tag]
        ids.append(backend.store(f"bulk memory {label} number {i} " + "filler " * 2000, meta))
    backend.close()
    for mem_id in ids:
        _shift_recorded(config, mem_id, days_ago)
    return ids


def test_main_pressure_archives_most_useless_first(
    make_config: Callable[..., Config],
) -> None:
    cfg = make_config(archival={"main_max_mb": 0.08, "archive_max_mb": 0})  # tiny cap, no purge
    useless = _bulk_store(cfg, 6, "useless")
    useful = _bulk_store(cfg, 2, "useful")
    kept = _bulk_store(cfg, 2, "kept", tag="keep")
    _seed_stats(
        cfg,
        [(m, 10, 0, 10, 0) for m in useless]  # injected plenty, never referenced
        + [(m, 10, 8, 2, 0) for m in useful],  # demonstrably useful
    )
    # mark useful as recently referenced so the recency guard protects them
    conn = sqlite3.connect(str(cfg.ledger_db_path))
    with conn:
        for m in useful:
            conn.execute(
                "UPDATE stats SET last_referenced = ? WHERE memory_id = ?",
                (datetime.now(UTC).isoformat(), m),
            )
    conn.close()

    report = run_archival(cfg)
    assert set(report["pressure"]) <= set(useless)  # only evidenced-useless moved
    assert report["pressure"], "pressure should have archived something"
    backend = get_backend(cfg)
    try:
        remaining = backend.fetch(useful + kept)
        assert len(remaining) == 4  # useful + keep-tagged all still resolvable
        live = {h.id for h in backend.search("bulk memory number", k=50, threshold=0.0)}
        assert set(kept) <= live  # keep tag never pressure-archived (A1)
        assert set(useful) <= live
    finally:
        backend.close()


def test_main_pressure_stops_rather_than_archive_useful(
    make_config: Callable[..., Config],
) -> None:
    cfg = make_config(archival={"main_max_mb": 0.01, "archive_max_mb": 0})
    _bulk_store(cfg, 3, "kept", tag="keep")  # everything protected → no candidates
    report = run_archival(cfg)
    assert report["pressure"] == []
    assert "refusing" in report["note"]


# --- regime 3: archive pressure (permanent, FIFO) ----------------------------------------
def test_archive_pressure_purges_fifo_forever(make_config: Callable[..., Config]) -> None:
    cfg = make_config(archival={"main_max_mb": 200, "archive_max_mb": 0.02})
    ids = _bulk_store(cfg, 6, "fifo", days_ago=200)
    backend = get_backend(cfg)
    first_archived = backend.archive_versions(ids[:3])  # older batch archived first
    backend.archive_versions(ids[3:])
    backend.close()

    report = run_archival(cfg)
    purged_ids = [mid for mid, _ in report["purged"]]
    assert purged_ids, "archive over cap must purge"
    assert purged_ids[0] in first_archived  # FIFO: oldest archived go first

    backend = get_backend(cfg)
    try:
        assert backend.fetch(purged_ids) == {}  # gone forever
        assert backend.verify_provenance()["ok"]  # purged events keep the chain consistent
    finally:
        backend.close()


def test_purge_disabled_with_zero_cap(make_config: Callable[..., Config]) -> None:
    cfg = make_config(archival={"archive_max_mb": 0})
    ids = _bulk_store(cfg, 3, "zerocap", days_ago=200)
    backend = get_backend(cfg)
    backend.archive_versions(ids)
    backend.close()
    report = run_archival(cfg)
    assert report["purged"] == []  # 0 = never delete (A5)


# --- integrity + plumbing ----------------------------------------------------------------
def test_tampered_archive_row_fails_verify(
    config: Config, aged_supersession: dict[str, str]
) -> None:
    run_archival(config)
    conn = sqlite3.connect(str(config.archive_db_path))
    with conn:
        conn.execute(
            "UPDATE memories SET text = 'rewritten history' WHERE id = ?",
            (aged_supersession["old"],),
        )
    conn.close()
    backend = SqliteFtsBackend(config)
    try:
        report = backend.verify_provenance()
        assert not report["ok"] and "archive" in report["reason"]
    finally:
        backend.close()


def test_dry_run_moves_nothing(config: Config, aged_supersession: dict[str, str]) -> None:
    report = run_archival(config, dry_run=True)
    assert report["steady"] == [aged_supersession["old"]]
    backend = get_backend(config)
    try:
        assert backend.health()["versions"] == 2  # untouched
    finally:
        backend.close()


def test_cli_archive_and_list(
    config: Config, aged_supersession: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert commands.cmd_archive(config) == 0
    out = capsys.readouterr().out
    assert "steady tier" in out and "moved 1" in out
    assert commands.cmd_archive(config, list_mode=True) == 0
    assert aged_supersession["old"] in capsys.readouterr().out
