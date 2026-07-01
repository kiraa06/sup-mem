"""Register the hook + MCP server into Claude Code config — NON-CLOBBERING (HANDOVER §7).

Confirmed layout (Claude Code, 2026):
  * Hooks live in ``~/.claude/settings.json`` under ``hooks`` (``UserPromptSubmit`` stdout is
    injected into context; matcher is optional for these events).
  * User-scoped MCP servers live in ``~/.claude/.mcp.json`` under ``mcpServers``.
  * Claude Code does NOT hot-reload — a restart is required to pick up the changes.

Every write deep-merges into existing config (preserving the user's other keys/hooks/servers),
dedupes our own entries (idempotent), and keeps a ``.claude-memory.bak`` of the prior file.
``claude_dir`` is overridable (and honors ``CLAUDE_CONFIG_DIR``) so this is testable.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_memory.config import Config

# Claude Code hook event → the console script that services it.
HOOK_EVENTS: dict[str, str] = {
    "UserPromptSubmit": "claude-memory-hook-userprompt",
    "SessionStart": "claude-memory-hook-session",
}
MCP_SERVER_NAME = "claude-memory"


def claude_config_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _resolve_command(name: str) -> str:
    """Absolute path to a console script if resolvable now, else the bare name (relies on PATH)."""
    return shutil.which(name) or name


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # unreadable/invalid — the backup on write preserves the original bytes
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".claude-memory.bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _entries_have_command(entries: list[Any], *commands: str) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") in commands:
                return True
    return False


def _merge_hooks(settings_path: Path) -> bool:
    data = _load_json(settings_path)
    if not isinstance(data.get("hooks"), dict):
        data["hooks"] = {}
    hooks = data["hooks"]
    changed = False
    for event, script in HOOK_EVENTS.items():
        command = _resolve_command(script)
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        if _entries_have_command(entries, command, script):
            continue  # already registered (idempotent, tolerant of bare-name vs abs-path)
        entries.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if changed:
        _atomic_write_json(settings_path, data)
    return changed


def _merge_mcp(mcp_path: Path) -> bool:
    data = _load_json(mcp_path)
    if not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    servers = data["mcpServers"]
    desired = {"command": _resolve_command(MCP_SERVER_NAME), "args": ["serve"]}
    if servers.get(MCP_SERVER_NAME) == desired:
        return False
    servers[MCP_SERVER_NAME] = desired
    _atomic_write_json(mcp_path, data)
    return True


def register_into_claude_code(config: Config, *, claude_dir: Path | None = None) -> dict[str, Any]:
    """Merge our hooks + MCP server into Claude Code config. Returns a report of what changed."""
    directory = claude_config_dir(claude_dir)
    directory.mkdir(parents=True, exist_ok=True)
    settings_path = directory / "settings.json"
    mcp_path = directory / ".mcp.json"
    return {
        "config_dir": str(directory),
        "settings_path": str(settings_path),
        "mcp_path": str(mcp_path),
        "hooks_changed": _merge_hooks(settings_path),
        "mcp_changed": _merge_mcp(mcp_path),
        "mcp_command": _resolve_command(MCP_SERVER_NAME),
    }
