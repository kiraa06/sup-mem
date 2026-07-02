"""Tamper-evident provenance chain (PHASE8 acceptance 6)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from sup_mem import commands
from sup_mem.backends import get_backend
from sup_mem.config import Config


def _seed(config: Config) -> str:
    backend = get_backend(config)
    mem_id = backend.store("the deploy pipeline uses blue-green", {"source": "session:x"})
    backend.store("the postgres pool is capped at fifty", {"source": "session:y"})
    backend.close()
    return mem_id


def test_untouched_store_verifies_green(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(config)
    assert commands.cmd_verify(config) == 0
    out = capsys.readouterr().out
    assert "intact" in out
    assert config.provenance_key_path.exists()  # key auto-created, 0600
    assert oct(config.provenance_key_path.stat().st_mode)[-3:] == "600"


def test_direct_text_edit_is_detected(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    mem_id = _seed(config)
    conn = sqlite3.connect(str(config.db_path))
    with conn:  # simulate an attacker editing a memory outside sup-mem's write path
        conn.execute(
            "UPDATE memories SET text = 'the deploy pipeline uses cowboy pushes' WHERE id = ?",
            (mem_id,),
        )
    conn.close()

    assert commands.cmd_verify(config) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and mem_id in out


def test_deleted_chain_event_is_detected(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(config)
    conn = sqlite3.connect(str(config.db_path))
    with conn:  # remove an event from the middle of the chain
        conn.execute("DELETE FROM provenance WHERE seq = 1")
    conn.close()

    assert commands.cmd_verify(config) == 1
    assert "chain break" in capsys.readouterr().out


def test_supersede_and_revive_events_recorded(config: Config) -> None:
    backend = get_backend(config)
    src = "native:proj/file.md"
    old = backend.store("version one of the fact", {"source": src})
    backend.store("version two of the fact", {"source": src})  # supersedes
    backend.store("version one of the fact", {"source": src})  # revives old
    backend.close()

    conn = sqlite3.connect(str(config.db_path))
    events = [
        (row[0], row[1])
        for row in conn.execute("SELECT event, memory_id FROM provenance ORDER BY seq")
    ]
    conn.close()
    assert ("superseded", old) in events
    assert ("revived", old) in events
    kinds = [e for e, _ in events]
    assert kinds.count("stored") == 2


def test_provenance_disabled_is_clean(
    make_config: Callable[..., Config], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(provenance={"enabled": False})
    backend = get_backend(cfg)
    backend.store("a fact with provenance off", {"source": "s"})
    backend.close()
    assert not cfg.provenance_key_path.exists()  # no key created when disabled
    assert commands.cmd_verify(cfg) == 0
    assert "disabled" in capsys.readouterr().out


def test_metadata_refresh_appends_restated_once(config: Config) -> None:
    backend = get_backend(config)
    backend.store("stable text", {"source": "session:x", "tags": ["a"]})
    backend.store("stable text", {"source": "session:x", "tags": ["a", "b"]})  # changed meta
    backend.store("stable text", {"source": "session:x", "tags": ["a", "b"]})  # identical
    backend.close()
    conn = sqlite3.connect(str(config.db_path))
    restated = conn.execute("SELECT COUNT(*) FROM provenance WHERE event = 'restated'").fetchone()[
        0
    ]
    conn.close()
    assert restated == 1  # only the actual change chained; the no-op re-store is silent
