"""Concurrency + hot-path robustness of the SQLite store, hook and MCP tools (0.9.4 review)."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from sup_mem import commands
from sup_mem.backends import get_backend
from sup_mem.backends.sqlite_fts import SqliteFtsBackend
from sup_mem.config import Config


def _live_per_source(config: Config) -> dict[str, int]:
    conn = sqlite3.connect(str(config.db_path))
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM memories WHERE superseded_at IS NULL GROUP BY source"
        ).fetchall()
    finally:
        conn.close()
    return {str(src): int(n) for src, n in rows}


def test_opening_a_current_store_takes_no_write_lock(config: Config) -> None:
    get_backend(config).close()  # create + migrate once
    blocker = sqlite3.connect(str(config.db_path), timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")  # another process mid-write (Stop hook, capture, MCP)
    try:
        started = time.monotonic()
        backend = get_backend(config)  # what every prompt does
        try:
            backend.search("anything", k=3, threshold=0.0)
        finally:
            backend.close()
        assert time.monotonic() - started < 1.0  # used to wait out the 5 s busy timeout
    finally:
        blocker.rollback()
        blocker.close()


_WRITER = """
import sys
from sup_mem.backends import get_backend
from sup_mem.config import load_config
backend = get_backend(load_config(overrides={"data_dir": sys.argv[1]}))
for i in range(80):
    src = f"session:s:topic-{i}"
    backend.store(f"version {sys.argv[2]} of the fact behind {src}", {"source": src})
backend.close()
"""


def test_concurrent_stores_leave_one_live_version_per_source(config: Config) -> None:
    # Separate PROCESSES, like the prompt hook, MCP server and capture child: the in-process
    # lock cannot help there — only BEGIN IMMEDIATE around the predecessor lookup does.
    import subprocess
    import sys

    get_backend(config).close()
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(config.data_dir), str(tag)])
        for tag in range(6)
    ]
    assert all(proc.wait(timeout=60) == 0 for proc in procs)
    live = _live_per_source(config)
    assert len(live) == 80
    assert all(n == 1 for n in live.values()), {s: n for s, n in live.items() if n != 1}


def test_restore_over_a_restored_id_keeps_the_fts_index_sound(config: Config) -> None:
    backend = get_backend(config)
    assert isinstance(backend, SqliteFtsBackend)
    try:
        mem = backend.store("gamma kiwi notes about the orchard", {"source": "native:x/a.md"})
        backend.archive_versions([mem])
        again = backend.store("gamma kiwi notes about the orchard", {"source": "native:x/a.md"})
        assert again == mem  # same text + source → same id, now live in main again
        assert backend.restore_versions([mem]) == 0  # main copy stands; nothing replaced
        with backend._conn:  # raises "database disk image is malformed" on an orphaned row
            backend._conn.execute(
                "INSERT INTO memories_fts(memories_fts) VALUES ('integrity-check')"
            )
        backend.archive_versions([mem])
        backend.store("wombat burrow survey", {"source": "native:x/b.md"})
        texts = [h.text for h in backend.search("wombat", k=5, threshold=0.0)]
        assert texts == ["wombat burrow survey"]  # no phantom match through an orphaned rowid
    finally:
        backend.close()


def test_sweep_does_not_revive_archived_native_memories(config: Config, tmp_path: Path) -> None:
    from sup_mem.migrate import migrate_native

    projects = tmp_path / "projects" / "-Users-me-proj" / "memory"
    projects.mkdir(parents=True)
    (projects / "deploy.md").write_text("Deploys go through the blue-green pipeline.\n")
    backend = get_backend(config)
    try:
        assert migrate_native(backend, tmp_path / "projects")["new"] == 1
        (mem,) = [h.id for h in backend.search("blue-green pipeline", k=1, threshold=0.0)]
        backend.archive_versions([mem])
        assert migrate_native(backend, tmp_path / "projects")["new"] == 0  # stays archived
        assert backend.search("blue-green pipeline", k=1, threshold=0.0) == []
    finally:
        backend.close()


def test_backup_includes_the_archive_tier(config: Config) -> None:
    from sup_mem import maintenance

    backend = get_backend(config)
    try:
        mem = backend.store("an old belief worth archiving", {"source": "native:x/c.md"})
        backend.archive_versions([mem])
    finally:
        backend.close()
    result = maintenance.backup_stores(config)
    assert result.status == "ok" and "archive.db" in result.detail
    (snapshot,) = [d for d in config.backups_dir.iterdir() if d.is_dir()]
    assert (snapshot / "archive.db").exists()


def test_as_of_with_an_offset_is_compared_in_utc() -> None:
    assert commands._parse_as_of("2026-06-01T10:00:00+05:30").startswith("2026-06-01T04:30:00")


def test_recall_ranks_by_outcomes_before_gating(make_config: Callable[..., Config]) -> None:
    from sup_mem.ledger import Ledger
    from sup_mem.mcp import server as mcp_server

    cfg = make_config(backend="sqlite_fts", retrieval={"threshold": 0.5, "k": 2})
    tools = mcp_server.MemoryTools(cfg)
    try:
        backend = tools._backend
        bad = backend.store("deploy pipeline note that keeps being contradicted", {})
        for i in range(3):
            backend.store(f"deploy pipeline fact number {i} that is fine", {})
        Ledger(cfg.ledger_db_path).close()
        conn = sqlite3.connect(str(cfg.ledger_db_path))
        with conn:  # quarantined: contradicted 3x, never redeemed
            conn.execute(
                "INSERT INTO stats (memory_id, injected, referenced, ignored, contradicted) "
                "VALUES (?, 4, 0, 1, 3)",
                (bad,),
            )
        conn.close()
        out = tools.recall("deploy pipeline", k=2)
        assert "contradicted" not in out
        assert out.count("fine") == 2  # quarantine does not shrink the answer below k
    finally:
        tools.close()


def test_prompt_hook_survives_a_non_utf8_pinned_file(
    config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sup_mem.hook import user_prompt_submit as hook

    backend = get_backend(config)
    backend.store("The database schema migration to Postgres 15 shipped in June.", {})
    backend.close()
    config.pinned_facts_path.write_bytes(b"caf\xe9 \xff\xfe not utf-8")
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(config.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "postgres schema?"})))
    assert hook.main() == 0  # used to traceback + exit 1 on every prompt
    assert "Postgres 15" in capsys.readouterr().out  # and retrieval still ran


def test_verify_backfills_genesis_for_rows_stored_without_provenance(
    make_config: Callable[..., Config],
) -> None:
    plain = get_backend(make_config(provenance={"enabled": False}))
    plain.store("written while provenance was off", {})
    plain.close()
    audited = get_backend(make_config())
    try:
        assert audited.verify_provenance()["ok"]  # genesis backfilled, not "missing event"
    finally:
        audited.close()
