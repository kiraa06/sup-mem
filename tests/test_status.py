"""status: the integration wiring check (Phase 7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sup_mem import commands, service
from sup_mem.backends import get_backend
from sup_mem.config import Config
from sup_mem.registration import HOOK_EVENTS
from sup_mem.status import collect_checks


def _wired_claude_dir(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A fake ~/.claude with all hooks + MCP registered, pointing at an existing binary."""
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    binary = tmp_path / "bin" / "sup-mem"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    hooks = {
        event: [{"hooks": [{"type": "command", "command": f"{binary.parent}/{script}"}]}]
        for event, script in HOOK_EVENTS.items()
    }
    for script in HOOK_EVENTS.values():
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
    plist = tmp_path / "plist"
    plist.write_text("x")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
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
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "absent")

    checks = {
        c.name: c for c in collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
    }
    assert not checks["hook:Stop"].ok
    assert "sup-mem init" in checks["hook:Stop"].fix
    assert not checks["service"].ok  # plist absent → actionable

    rc = commands.cmd_status(config, claude_dir=claude_dir, claude_json=claude_json)
    assert rc == 1


def test_registered_but_missing_binary_is_flagged(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir, claude_json, binary = _wired_claude_dir(tmp_path)
    binary.unlink()  # MCP command now dangles
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "absent")
    checks = {
        c.name: c for c in collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
    }
    assert not checks["mcp:sup-mem"].ok
    assert "missing" in checks["mcp:sup-mem"].detail
