"""status: the integration wiring check (Phase 7)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sup_mem import commands, service
from sup_mem.backends import get_backend
from sup_mem.config import Config
from sup_mem.registration import CLAUDE_EVENTS, SCRIPTS
from sup_mem.status import Check, collect_checks


def _wired_claude_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A fake ~/.claude with all hooks + MCP registered, pointing at an existing binary."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    binary = tmp_path / "bin" / "sup-mem"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    hooks = {
        CLAUDE_EVENTS[role]: [
            {"hooks": [{"type": "command", "command": f"{binary.parent}/{script}"}]}
        ]
        for role, script in SCRIPTS.items()
    }
    for script in SCRIPTS.values():
        (binary.parent / script).write_text("#!/bin/sh\n")
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": hooks}))
    claude_json = claude_dir / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"sup-mem": {"command": str(binary), "args": ["serve"]}}})
    )
    return claude_dir, claude_json, binary


def test_all_wired_reports_green(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir, claude_json, _ = _wired_claude_dir(tmp_path)
    backend = get_backend(config)
    backend.store("a memory", {"source": "s"})
    backend.close()
    monkeypatch.setattr(service, "scheduler_kind", lambda: "launchd")
    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "loaded", lambda runner=None: True)

    checks = {
        c.name: c for c in collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
    }
    for name in (
        "hook:UserPromptSubmit",
        "hook:SessionStart",
        "hook:Stop",
        "mcp:sup-mem",
        "store",
        "service",
    ):
        assert checks[name].ok, f"{name}: {checks[name].detail}"

    rc = commands.cmd_status(config, claude_dir=claude_dir, claude_json=claude_json)
    assert rc == 0


def test_missing_hook_is_flagged_with_fix(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir, claude_json, _ = _wired_claude_dir(tmp_path)
    settings = json.loads((claude_dir / "settings.json").read_text())
    del settings["hooks"]["Stop"]  # simulate registration drift
    (claude_dir / "settings.json").write_text(json.dumps(settings))
    monkeypatch.setattr(service, "scheduler_kind", lambda: "systemd")
    monkeypatch.setattr(service, "installed", lambda: False)

    checks = {
        c.name: c for c in collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
    }
    assert not checks["hook:Stop"].ok
    assert "sup-mem init" in checks["hook:Stop"].fix
    assert not checks["service"].ok  # scheduler available but nothing installed → actionable

    rc = commands.cmd_status(config, claude_dir=claude_dir, claude_json=claude_json)
    assert rc == 1


def test_registered_but_missing_binary_is_flagged(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir, claude_json, binary = _wired_claude_dir(tmp_path)
    binary.unlink()  # MCP command now dangles
    monkeypatch.setattr(service, "scheduler_kind", lambda: "none")
    checks = {
        c.name: c for c in collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
    }
    assert not checks["mcp:sup-mem"].ok
    assert "missing" in checks["mcp:sup-mem"].detail
    assert checks["service"].ok  # no scheduler on this host → informational, not red
    assert "cron" in checks["service"].detail


def _write_capture_log(config: Config, *records: tuple[str, str]) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    lines = [json.dumps({"ts": now, "status": s, "detail": d}) for s, d in records]
    (config.logs_dir / "capture.log").write_text("\n".join(lines) + "\n")


def test_capture_failures_warn_with_fix_but_stay_wired(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a dead `claude` login failed every capture and nothing surfaced it.
    claude_dir, claude_json, _ = _wired_claude_dir(tmp_path)
    monkeypatch.setattr(service, "scheduler_kind", lambda: "none")

    def capture_check() -> Check:
        checks = collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
        return next(c for c in checks if c.name == "capture")

    quiet = capture_check()  # no capture.log yet → informational
    assert quiet.ok and not quiet.warn and "no captures" in quiet.detail

    _write_capture_log(config, ("ok", ""), ("empty", ""))
    healthy = capture_check()
    assert healthy.ok and not healthy.warn and not healthy.fix
    assert healthy.detail.startswith("2 ok in 7d · last ") and healthy.detail.endswith(" empty")

    expired = "Failed to authenticate: OAuth session expired and could not be refreshed"
    _write_capture_log(config, ("ok", ""), ("auth", expired))
    broken = capture_check()
    assert broken.ok and broken.warn  # a warning, not a wiring failure (like last-maintain)
    assert broken.fix == "run `claude auth login` in a terminal"
    assert "1 of 2 failed in 7d" in broken.detail and expired in broken.detail
    assert commands.cmd_status(config, claude_dir=claude_dir, claude_json=claude_json) == 0

    _write_capture_log(config, ("timeout", "no reply within 90s"))
    assert "capture.timeout_seconds" in capture_check().fix

    # Failures followed by a success are history, not an alarm (the 10-29 outage recovered
    # by 10-30; a 7-day warning after that would be noise).
    _write_capture_log(config, ("auth", expired), ("ok", ""))
    recovered = capture_check()
    assert not recovered.warn and not recovered.fix and "1 of 2 failed" in recovered.detail


def test_capture_disabled_is_reported(
    make_config: Callable[..., Config], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(capture={"enabled": False})
    _write_capture_log(cfg, ("auth", "Failed to authenticate"))  # a stale log is not read
    claude_dir, claude_json, _ = _wired_claude_dir(tmp_path)
    monkeypatch.setattr(service, "scheduler_kind", lambda: "none")
    checks = {
        c.name: c for c in collect_checks(cfg, claude_dir=claude_dir, claude_json=claude_json)
    }
    assert checks["capture"].ok and not checks["capture"].warn
    assert "disabled" in checks["capture"].detail
