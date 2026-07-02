"""Register the hook + MCP server into Claude Code config — NON-CLOBBERING (HANDOVER §7).

Layout (Claude Code, 2026 — verified on-machine):
  * Hooks live in ``~/.claude/settings.json`` under ``hooks`` (``UserPromptSubmit`` stdout is
    injected into context; matcher is optional for these events).
  * User-scoped MCP servers live in ``~/.claude.json`` under ``mcpServers``. We register them
    the canonical way — ``claude mcp add --scope user`` — which writes to the right place
    regardless of version, and fall back to a direct non-clobbering merge of ``~/.claude.json``
    only when the ``claude`` binary isn't on PATH.
  * Claude Code does NOT hot-reload — a restart is required to pick up the changes.

Hook writes deep-merge into existing config (preserving the user's other keys/hooks), dedupe
our own entries (idempotent), and keep a ``.sup-mem.bak`` of the prior file.
``claude_dir`` / ``claude_json`` are overridable (and honor ``CLAUDE_CONFIG_DIR``) for testing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config

HOOK_EVENTS: dict[str, str] = {
    "UserPromptSubmit": "sup-mem-hook-userprompt",
    "SessionStart": "sup-mem-hook-session",
}
MCP_SERVER_NAME = "sup-mem"


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
        shutil.copy2(path, path.with_name(path.name + ".sup-mem.bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------------------------
# Hooks → ~/.claude/settings.json
# --------------------------------------------------------------------------------------------
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
            continue  # already registered (tolerant of bare-name vs abs-path)
        entries.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if changed:
        _atomic_write_json(settings_path, data)
    return changed


# --------------------------------------------------------------------------------------------
# MCP server → ~/.claude.json (via `claude mcp add`, or a direct merge fallback)
# --------------------------------------------------------------------------------------------
def _mcp_command() -> str:
    return _resolve_command(MCP_SERVER_NAME)


def _register_mcp_via_cli(command: str) -> bool | None:
    """Register with `claude mcp add --scope user`. Returns True/False if handled (changed?),
    or None if the `claude` binary isn't available (caller should fall back)."""
    claude = shutil.which("claude")
    if not claude:
        return None
    existing = subprocess.run(
        [claude, "mcp", "get", MCP_SERVER_NAME], capture_output=True, text=True
    )
    if existing.returncode == 0 and command in existing.stdout:
        return False  # already registered with our command → idempotent
    subprocess.run(
        [claude, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"], capture_output=True, text=True
    )  # clear any stale entry; ignore failure if absent
    added = subprocess.run(
        [claude, "mcp", "add", MCP_SERVER_NAME, "--scope", "user", "--", command, "serve"],
        capture_output=True,
        text=True,
    )
    return added.returncode == 0


def _merge_mcp_json(json_path: Path, command: str) -> bool:
    """Non-clobbering merge into a Claude config file's top-level ``mcpServers``."""
    data = _load_json(json_path)
    if not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    servers = data["mcpServers"]
    desired = {"command": command, "args": ["serve"]}
    if servers.get(MCP_SERVER_NAME) == desired:
        return False
    servers[MCP_SERVER_NAME] = desired
    _atomic_write_json(json_path, data)
    return True


def register_into_claude_code(
    config: Config,
    *,
    claude_dir: Path | None = None,
    claude_json: Path | None = None,
    use_cli: bool = True,
) -> dict[str, Any]:
    """Merge our hooks + MCP server into Claude Code config. Returns a report of what changed."""
    directory = claude_config_dir(claude_dir)
    directory.mkdir(parents=True, exist_ok=True)
    settings_path = directory / "settings.json"
    hooks_changed = _merge_hooks(settings_path)

    command = _mcp_command()
    mcp_json = claude_json if claude_json is not None else Path.home() / ".claude.json"
    cli_result = _register_mcp_via_cli(command) if use_cli else None
    if cli_result is not None:
        mcp_changed = cli_result
        mcp_target = "claude mcp (user scope → ~/.claude.json)"
    else:
        mcp_changed = _merge_mcp_json(mcp_json, command)
        mcp_target = str(mcp_json)

    return {
        "config_dir": str(directory),
        "settings_path": str(settings_path),
        "mcp_target": mcp_target,
        "hooks_changed": hooks_changed,
        "mcp_changed": mcp_changed,
        "mcp_command": command,
    }
