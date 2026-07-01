"""CLI init + non-clobbering Claude Code registration (HANDOVER §7).

``use_cli=False`` forces the deterministic file-merge path so tests never shell out to the
real ``claude`` binary (which would touch the developer's ~/.claude.json).
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_memory import commands
from claude_memory.config import load_config
from claude_memory.registration import register_into_claude_code


def test_init_creates_store_config_pinned_and_registers(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    claude_dir = tmp_path / "claude"
    claude_json = claude_dir / ".claude.json"
    cfg = load_config(overrides={"data_dir": str(data_dir)})

    rc = commands.cmd_init(cfg, claude_dir=claude_dir, claude_json=claude_json, use_cli=False)
    assert rc == 0

    assert (data_dir / "memory.db").exists()
    assert (data_dir / "config.toml").exists()
    assert (data_dir / "pinned.md").exists()

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert "UserPromptSubmit" in settings["hooks"]
    assert "SessionStart" in settings["hooks"]

    mcp = json.loads(claude_json.read_text())
    assert mcp["mcpServers"]["claude-memory"]["args"] == ["serve"]


def test_registration_merges_without_clobbering_and_is_idempotent(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    claude_json = claude_dir / ".claude.json"

    # Pre-existing settings with the user's own hook, an unrelated hook event, and other keys.
    settings = {
        "model": "opus",
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "user-own-hook"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}],
        },
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings))
    # Pre-existing ~/.claude.json with another MCP server + an unrelated top-level key.
    claude_json.write_text(
        json.dumps({"numStartups": 42, "mcpServers": {"codegraph": {"command": "codegraph"}}})
    )

    cfg = load_config(overrides={"data_dir": str(tmp_path / "data")})
    first = register_into_claude_code(
        cfg, claude_dir=claude_dir, claude_json=claude_json, use_cli=False
    )
    assert first["hooks_changed"] and first["mcp_changed"]

    merged = json.loads((claude_dir / "settings.json").read_text())
    assert merged["model"] == "opus"  # unrelated keys preserved
    assert merged["permissions"] == {"allow": ["Bash"]}
    assert merged["hooks"]["PreToolUse"][0]["matcher"] == "Bash"  # other hook event preserved
    ups = [h["command"] for e in merged["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert "user-own-hook" in ups  # the user's own hook survives
    assert any("claude-memory-hook-userprompt" in c for c in ups)  # ours added alongside

    mcp = json.loads(claude_json.read_text())
    assert mcp["numStartups"] == 42  # unrelated top-level key preserved
    assert "codegraph" in mcp["mcpServers"] and "claude-memory" in mcp["mcpServers"]

    # Backups of the pre-existing files were kept.
    assert (claude_dir / "settings.json.claude-memory.bak").exists()
    assert claude_json.with_name(".claude.json.claude-memory.bak").exists()

    # Re-running changes nothing (idempotent).
    second = register_into_claude_code(
        cfg, claude_dir=claude_dir, claude_json=claude_json, use_cli=False
    )
    assert not second["hooks_changed"] and not second["mcp_changed"]
