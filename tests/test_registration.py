"""CLI init + non-clobbering Claude Code registration (HANDOVER §7)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_memory import commands
from claude_memory.config import load_config
from claude_memory.registration import register_into_claude_code


def test_init_creates_store_config_pinned_and_registers(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    claude_dir = tmp_path / "claude"
    cfg = load_config(overrides={"data_dir": str(data_dir)})

    assert commands.cmd_init(cfg, claude_dir=claude_dir) == 0

    assert (data_dir / "memory.db").exists()
    assert (data_dir / "config.toml").exists()
    assert (data_dir / "pinned.md").exists()

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert "UserPromptSubmit" in settings["hooks"]
    assert "SessionStart" in settings["hooks"]

    mcp = json.loads((claude_dir / ".mcp.json").read_text())
    assert mcp["mcpServers"]["claude-memory"]["args"] == ["serve"]


def test_registration_merges_without_clobbering_and_is_idempotent(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    # Pre-existing user config with their own hook, an unrelated hook event, and other keys.
    settings = {
        "model": "opus",
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "user-own-hook"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}],
        },
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings))
    (claude_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "o"}}}))

    cfg = load_config(overrides={"data_dir": str(tmp_path / "data")})
    first = register_into_claude_code(cfg, claude_dir=claude_dir)
    assert first["hooks_changed"] and first["mcp_changed"]

    merged = json.loads((claude_dir / "settings.json").read_text())
    assert merged["model"] == "opus"  # unrelated keys preserved
    assert merged["permissions"] == {"allow": ["Bash"]}
    assert merged["hooks"]["PreToolUse"][0]["matcher"] == "Bash"  # other hook event preserved
    ups = [h["command"] for e in merged["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert "user-own-hook" in ups  # the user's own hook survives
    assert any("claude-memory-hook-userprompt" in c for c in ups)  # ours added alongside

    mcp = json.loads((claude_dir / ".mcp.json").read_text())
    assert "other" in mcp["mcpServers"] and "claude-memory" in mcp["mcpServers"]

    # A backup of the pre-existing file was kept.
    assert (claude_dir / "settings.json.claude-memory.bak").exists()

    # Re-running changes nothing (idempotent).
    second = register_into_claude_code(cfg, claude_dir=claude_dir)
    assert not second["hooks_changed"] and not second["mcp_changed"]
