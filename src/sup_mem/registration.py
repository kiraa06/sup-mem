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

# The four hook roles, each backed by one console script (shared across every client — the
# stdin/stdout contract is identical; only the event *names* and config *location* differ).
SCRIPTS: dict[str, str] = {
    "user_prompt_submit": "sup-mem-hook-userprompt",
    "session_start": "sup-mem-hook-session",
    "stop": "sup-mem-hook-stop",  # the outcome loop's attribution pass (PHASE6)
    "pre_compact": "sup-mem-hook-precompact",  # the compaction lifeboat (PHASE10)
}
# Per-role timeouts (seconds); PreCompact runs a headless model call.
TIMEOUTS: dict[str, int] = {"pre_compact": 120}

# canonical role -> event name in each client's config. Claude Code and Codex share the exact
# event vocabulary (Codex cloned Claude's hook contract, JSON envelope and all); Gemini renames
# them. Hook I/O is otherwise identical, so one script set serves all three.
CLAUDE_EVENTS: dict[str, str] = {
    "user_prompt_submit": "UserPromptSubmit",
    "session_start": "SessionStart",
    "stop": "Stop",
    "pre_compact": "PreCompact",
}
CODEX_EVENTS: dict[str, str] = dict(CLAUDE_EVENTS)  # verbatim clone of Claude's event names
GEMINI_EVENTS: dict[str, str] = {
    "user_prompt_submit": "BeforeAgent",
    "session_start": "SessionStart",
    "stop": "AfterAgent",
    "pre_compact": "PreCompress",
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


def _hook_command(script: str, env_client: str | None) -> str:
    """Resolve the console script. For non-default clients, tag the invocation with the client
    name so the hook picks the right stdout dialect + transcript parser (``SUP_MEM_CLIENT``).
    Claude Code gets the bare command (no tag) — its path stays byte-identical to before."""
    command = _resolve_command(script)
    return f"env SUP_MEM_CLIENT={env_client} {command}" if env_client else command


def _merge_hooks_into(data: dict[str, Any], events: dict[str, str], env_client: str | None) -> bool:
    """Merge our hook entries into an in-memory Claude-Code-shaped ``hooks`` map. Returns changed?

    ``events`` maps canonical role -> this client's event name. Codex's ``hooks.json`` and
    Gemini's ``settings.json`` use the identical schema, so this one merger serves all three.
    Pure (no I/O) so callers writing several sections into one file can do a single write.
    """
    if not isinstance(data.get("hooks"), dict):
        data["hooks"] = {}
    hooks = data["hooks"]
    changed = False
    for role, event in events.items():
        script = SCRIPTS[role]
        command = _hook_command(script, env_client)
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        if _entries_have_command(entries, command, script):
            continue  # already registered (tolerant of bare-name vs abs-path vs env-prefix)
        hook_entry: dict[str, Any] = {"type": "command", "command": command}
        if role in TIMEOUTS:
            hook_entry["timeout"] = TIMEOUTS[role]
        entries.append({"hooks": [hook_entry]})
        changed = True
    return changed


def merge_hooks_json(settings_path: Path, events: dict[str, str], env_client: str | None) -> bool:
    """Non-clobbering, single-write merge of our hooks into a hooks JSON file (Claude, Codex)."""
    data = _load_json(settings_path)
    if _merge_hooks_into(data, events, env_client):
        _atomic_write_json(settings_path, data)
        return True
    return False


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


def _merge_mcp_into(data: dict[str, Any], command: str) -> bool:
    """Merge our server into an in-memory top-level ``mcpServers`` map. Returns changed? Pure."""
    if not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    servers = data["mcpServers"]
    desired = {"command": command, "args": ["serve"]}
    if servers.get(MCP_SERVER_NAME) == desired:
        return False
    servers[MCP_SERVER_NAME] = desired
    return True


def _merge_mcp_json(json_path: Path, command: str) -> bool:
    """Non-clobbering merge into a Claude config file's top-level ``mcpServers``."""
    data = _load_json(json_path)
    if _merge_mcp_into(data, command):
        _atomic_write_json(json_path, data)
        return True
    return False


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
    hooks_changed = merge_hooks_json(settings_path, CLAUDE_EVENTS, None)

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
        "client": "claude",
        "config_dir": str(directory),
        "settings_path": str(settings_path),
        "mcp_target": mcp_target,
        "hooks_changed": hooks_changed,
        "mcp_changed": mcp_changed,
        "mcp_command": command,
    }


# --------------------------------------------------------------------------------------------
# Codex CLI  →  ~/.codex/hooks.json (hooks) + ~/.codex/config.toml (MCP)
# --------------------------------------------------------------------------------------------
def _append_codex_mcp(config_toml: Path, command: str) -> tuple[bool, str]:
    """Append an ``[mcp_servers.sup-mem]`` table to Codex's config.toml if absent.

    Append-only + a ``.sup-mem.bak`` — never rewrites the user's existing TOML (no stdlib TOML
    writer to round-trip it safely). A fresh top-level table at EOF is unambiguous.
    """
    import tomllib

    existing: dict[str, Any] = {}
    if config_toml.exists():
        try:
            existing = tomllib.loads(config_toml.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    servers = existing.get("mcp_servers")
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        return False, str(config_toml)  # already registered → idempotent
    block = f'\n[mcp_servers.{MCP_SERVER_NAME}]\ncommand = "{command}"\nargs = ["serve"]\n'
    config_toml.parent.mkdir(parents=True, exist_ok=True)
    if config_toml.exists():
        shutil.copy2(config_toml, config_toml.with_name(config_toml.name + ".sup-mem.bak"))
    with config_toml.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return True, str(config_toml)


def register_into_codex(config: Config, *, codex_home: Path | None = None) -> dict[str, Any]:
    """Merge our hooks (``hooks.json``) + MCP server (``config.toml``) into Codex CLI config.
    Codex speaks Claude's exact hook dialect, so the same scripts/JSON envelope just work."""
    home = codex_home if codex_home is not None else Path.home() / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    hooks_path = home / "hooks.json"
    hooks_changed = merge_hooks_json(hooks_path, CODEX_EVENTS, "codex")
    command = _mcp_command()
    mcp_changed, mcp_target = _append_codex_mcp(home / "config.toml", command)
    return {
        "client": "codex",
        "config_dir": str(home),
        "settings_path": str(hooks_path),
        "mcp_target": mcp_target,
        "hooks_changed": hooks_changed,
        "mcp_changed": mcp_changed,
        "mcp_command": command,
    }


# --------------------------------------------------------------------------------------------
# Gemini CLI  →  ~/.gemini/settings.json (hooks + MCP, both in one JSON file)
# --------------------------------------------------------------------------------------------
def register_into_gemini(config: Config, *, gemini_home: Path | None = None) -> dict[str, Any]:
    """Merge our hooks + MCP server into Gemini CLI's settings.json (same JSON schema as Claude;
    event names are renamed via ``GEMINI_EVENTS`` and the hook emits the JSON-envelope dialect)."""
    home = gemini_home if gemini_home is not None else Path.home() / ".gemini"
    home.mkdir(parents=True, exist_ok=True)
    settings_path = home / "settings.json"
    # Hooks AND mcpServers both live in this one file — merge both in memory, then write ONCE,
    # so the single ``.bak`` is the pristine original (two writes would clobber the first backup).
    data = _load_json(settings_path)
    hooks_changed = _merge_hooks_into(data, GEMINI_EVENTS, "gemini")
    command = _mcp_command()
    mcp_changed = _merge_mcp_into(data, command)
    if hooks_changed or mcp_changed:
        _atomic_write_json(settings_path, data)
    return {
        "client": "gemini",
        "config_dir": str(home),
        "settings_path": str(settings_path),
        "mcp_target": str(settings_path),
        "hooks_changed": hooks_changed,
        "mcp_changed": mcp_changed,
        "mcp_command": command,
    }
