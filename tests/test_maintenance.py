"""maintain: rotation w/ exact cursor rebase, backups+retention, auto-tune, service (Phase 7)."""

from __future__ import annotations

import json
import plistlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sup_mem import maintenance, service
from sup_mem.backends import get_backend
from sup_mem.config import Config
from sup_mem.ledger import Ledger


def _log_line(session: str, days_old: int) -> str:
    ts = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    return json.dumps({"ts": ts, "session_id": session, "tier": "retrieve", "candidates": []})


def _set_cursor(db: Path, session: str, line: int) -> None:
    Ledger(db).close()
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO cursors (session_id, log_line) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET log_line = excluded.log_line",
            (session, line),
        )
    conn.close()


def _cursor(db: Path, session: str) -> int:
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT log_line FROM cursors WHERE session_id = ?", (session,)).fetchone()
    conn.close()
    return int(row[0])


# --- rotation -------------------------------------------------------------------------------
def test_rotate_drops_old_lines_and_rebases_cursors_exactly(config: Config) -> None:
    # lines: 0 old(A), 1 new(A), 2 old(B), 3 new(B), 4 new(A)  → dropped indexes [0, 2]
    lines = [
        _log_line("A", 30),
        _log_line("A", 0),
        _log_line("B", 30),
        _log_line("B", 0),
        _log_line("A", 0),
    ]
    config.retrieval_log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _set_cursor(config.ledger_db_path, "A", 5)  # fully processed
    _set_cursor(config.ledger_db_path, "B", 3)

    result = maintenance.rotate_log(config)
    assert result.status == "ok" and "dropped 2" in result.detail

    kept = config.retrieval_log_path.read_text().splitlines()
    assert len(kept) == 3 and all('"ts"' in line for line in kept)
    # both dropped indexes (0, 2) were below each cursor → shift by 2
    assert _cursor(config.ledger_db_path, "A") == 3
    assert _cursor(config.ledger_db_path, "B") == 1


def test_rotate_keeps_everything_within_window(config: Config) -> None:
    config.retrieval_log_path.write_text(_log_line("A", 1) + "\n", encoding="utf-8")
    result = maintenance.rotate_log(config)
    assert result.status == "ok" and "none past" in result.detail
    assert len(config.retrieval_log_path.read_text().splitlines()) == 1


def test_rotate_ages_out_legacy_lines_without_ts(config: Config) -> None:
    config.retrieval_log_path.write_text('{"query": "old format"}\n', encoding="utf-8")
    result = maintenance.rotate_log(config)
    assert result.status == "ok" and "dropped 1" in result.detail


# --- backups --------------------------------------------------------------------------------
def test_backup_snapshots_and_prunes(config: Config) -> None:
    backend = get_backend(config)
    backend.store("a memory to back up", {"source": "s"})
    backend.close()
    Ledger(config.ledger_db_path).close()

    result = maintenance.backup_stores(config)
    assert result.status == "ok"
    dirs = [d for d in config.backups_dir.iterdir() if d.is_dir()]
    assert len(dirs) == 1
    assert {f.name for f in dirs[0].iterdir()} == {"memory.db", "ledger.db"}
    # a backed-up snapshot is a valid, queryable database
    conn = sqlite3.connect(str(dirs[0] / "memory.db"))
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    conn.close()

    for i in range(9):  # fabricate older backups → retention keeps newest backup_keep (7)
        (config.backups_dir / f"2020010{i}-000000").mkdir()
    result = maintenance.backup_stores(config)
    assert result.status == "ok"
    assert len(list(config.backups_dir.iterdir())) == config.maintenance.backup_keep


# --- auto-tune ------------------------------------------------------------------------------
def _seed_candidate(db: Path, line: int, mem: str, score: float, outcome: str) -> None:
    Ledger(db).close()
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "INSERT INTO candidates "
            "(session_id, line_no, memory_id, score, injected, outcome, tokens) "
            "VALUES ('s', ?, ?, ?, 1, ?, 10)",
            (line, mem, score, outcome),
        )
    conn.close()


def test_auto_tune_needs_enough_evidence(config: Config) -> None:
    _seed_candidate(config.ledger_db_path, 0, "a", 0.8, "referenced")
    result = maintenance.auto_tune(config)  # default tune_min_attributed = 20
    assert result.status == "skipped" and "1/20" in result.detail


def test_auto_tune_applies_lossless_threshold(make_config: Callable[..., Config]) -> None:
    cfg = make_config(maintenance={"tune_min_attributed": 1})
    _seed_candidate(cfg.ledger_db_path, 0, "a", 0.8, "referenced")
    _seed_candidate(cfg.ledger_db_path, 1, "b", 0.4, "ignored")
    result = maintenance.auto_tune(cfg)
    assert result.status == "ok" and "applied" in result.detail
    assert "threshold = 0.8" in cfg.config_path.read_text(encoding="utf-8")


def test_auto_tune_respects_disable(make_config: Callable[..., Config]) -> None:
    cfg = make_config(maintenance={"auto_tune": False})
    assert maintenance.auto_tune(cfg).status == "skipped"


# --- the full run ---------------------------------------------------------------------------
def test_run_maintenance_is_green_and_idempotent(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # hermetic sweep
    backend = get_backend(config)
    backend.store("seed memory", {"source": "s", "tags": ["seed"]})
    backend.close()

    for _ in range(2):  # idempotent
        results = maintenance.run_maintenance(config)
        assert all(r.status != "failed" for r in results), [
            (r.name, r.detail) for r in results if r.status == "failed"
        ]
    assert config.maintain_stamp_path.exists()
    names = [r.name for r in maintenance.run_maintenance(config)]
    assert names == [
        "rotate",
        "backup",
        "sweep",
        "auto-tune",
        "manifest",
        "vacuum",
        "provenance",
        "health",
    ]


# --- launchd service ------------------------------------------------------------------------
def test_render_plist_contents(config: Config) -> None:
    payload = plistlib.loads(service.render_plist(config))
    assert payload["Label"] == "com.sup-mem.maintain"
    assert payload["ProgramArguments"][-1] == "maintain"
    assert payload["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert payload["StandardOutPath"].endswith("logs/maintain.log")
    assert payload["RunAtLoad"] is False


def _fake_runner(calls: list[list[str]], returncode: int = 0) -> Any:
    def run(args: list[str], **_kw: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    return run


def test_service_install_writes_plist_and_bootstraps(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist = tmp_path / "com.sup-mem.maintain.plist"
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    monkeypatch.setattr(service, "_is_macos", lambda: True)
    calls: list[list[str]] = []

    ok, message = service.install(config, runner=_fake_runner(calls))
    assert ok, message
    assert plist.exists()
    assert any(c[1] == "bootstrap" for c in calls)

    ok, message = service.uninstall(runner=_fake_runner(calls))
    assert ok and not plist.exists()
    assert any(c[1] == "bootout" for c in calls)


def test_service_install_without_any_scheduler_suggests_cron(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_is_macos", lambda: False)
    monkeypatch.setattr(service, "_is_linux", lambda: False)
    ok, message = service.install(config, runner=_fake_runner([]))
    assert not ok and "crontab" in message
    assert "30 3 * * *" in message


# --- systemd (Linux) --------------------------------------------------------------------------
def _force_systemd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(service, "_is_macos", lambda: False)
    monkeypatch.setattr(service, "_is_linux", lambda: True)
    monkeypatch.setattr(service, "_has_systemctl", lambda: True)
    unit_dir = tmp_path / "systemd-user"
    monkeypatch.setattr(service, "systemd_dir", lambda: unit_dir)
    return unit_dir


def test_systemd_unit_contents(config: Config) -> None:
    unit = service.render_systemd_service(config)
    timer = service.render_systemd_timer(config)
    assert "ExecStart=" in unit and unit.strip().endswith("maintain.log")
    assert "Type=oneshot" in unit
    assert "OnCalendar=*-*-* 03:30:00" in timer
    assert "Persistent=true" in timer


def test_systemd_install_writes_units_and_enables(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = _force_systemd(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    ok, message = service.install(config, runner=_fake_runner(calls))
    assert ok, message
    assert (unit_dir / "sup-mem-maintain.service").exists()
    assert (unit_dir / "sup-mem-maintain.timer").exists()
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert any(c[:4] == ["systemctl", "--user", "enable", "--now"] for c in calls)

    ok, message = service.uninstall(runner=_fake_runner(calls))
    assert ok
    assert not (unit_dir / "sup-mem-maintain.timer").exists()
    assert not (unit_dir / "sup-mem-maintain.service").exists()
    assert any(c[:4] == ["systemctl", "--user", "disable", "--now"] for c in calls)


def test_systemd_loaded_reflects_is_active(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_systemd(monkeypatch, tmp_path)
    assert service.loaded(runner=_fake_runner([], returncode=0)) is True
    assert service.loaded(runner=_fake_runner([], returncode=3)) is False
