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
our own entries (idempotent), and keep a ``.sup-mem.bak`` of the prior file. A config file we
cannot parse is never rewritten: that client's registration aborts with ``RegistrationError``.
``claude_dir`` / ``claude_json`` are overridable (and honor ``CLAUDE_CONFIG_DIR``) for testing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
# Per-role timeouts in SECONDS; PreCompact runs a headless model call. Claude Code and Codex
# read hook timeouts in seconds, but Gemini CLI reads MILLISECONDS (its DEFAULT_HOOK_TIMEOUT is
# 60000) — written as seconds, Gemini killed the capture hook after 120 ms. See ``timeout_ms``.
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
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")  # a leading ``VAR=value`` env token


class RegistrationError(RuntimeError):
    """A client's config could not be read/parsed — its registration aborts, nothing written.

    Merging into ``{}`` and writing back (the old behaviour) kept only our keys and wiped the
    user's settings; refusing with the file named is the only safe answer.
    """


def claude_config_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _in_venv(path: str) -> bool:
    return ".venv" in Path(path).parts


def _resolve_command(name: str) -> str:
    """Absolute path to a console script if resolvable now, else the bare name (relies on PATH).

    A script inside a project ``.venv`` dies with that venv, so the ``~/.local/bin`` shim
    (``uv tool install``) wins whenever it exists; otherwise ``_venv_warnings`` flags it.
    """
    found = shutil.which(name)
    if found and _in_venv(found):
        shim = Path.home() / ".local" / "bin" / name
        if shim.is_file() and os.access(shim, os.X_OK):
            return str(shim)
    return found or name


def _venv_warnings(mcp_command: str) -> list[str]:
    """One warning per project-venv directory our hook/MCP commands resolved into."""
    paths = [*(_resolve_command(script) for script in SCRIPTS.values()), mcp_command]
    return [
        f"sup-mem scripts resolve inside a project virtualenv ({directory}) — the hooks and "
        "MCP server break once that venv is removed. Install with `uv tool install sup-mem` "
        "and re-run `sup-mem init`."
        for directory in sorted({str(Path(p).parent) for p in paths if _in_venv(p)})
    ]


def _strip_json_comments(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments OUTSIDE string literals (JSONC → JSON).

    Gemini CLI's settings.json is JSONC (gemini-cli parses it with strip-json-comments), so a
    commented file is valid there. Newlines are kept so parse errors still name the right line.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":  # keep the escape and the escaped char (e.g. \")
                out.append(text[i : i + 2])
                i += 2
                continue
            in_string = ch != '"'
            out.append(ch)
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end  # the newline itself is kept
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            out.append("\n" * text.count("\n", i, stop) or " ")
            i = stop
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _load_json(
    path: Path, *, jsonc: bool = False, notes: list[str] | None = None
) -> dict[str, Any]:
    """Parse a client's JSON config. Missing/blank → ``{}``; anything else unparseable (or not
    a JSON object) raises ``RegistrationError`` naming the file — never ``{}``, because the
    merge-then-write that follows would keep only our keys and wipe the user's settings.

    ``jsonc`` strips comments first (Gemini); when it does, a note lands in ``notes`` — the
    rewritten file cannot carry them (the ``.sup-mem.bak`` keeps the original bytes).
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise RegistrationError(f"could not read {path} ({exc}) — nothing was written") from exc
    if jsonc:
        stripped = _strip_json_comments(raw)
        if stripped != raw and notes is not None:
            notes.append(
                f"comments in {path} are not kept in the rewritten file "
                f"(the original is saved as {path.name}.sup-mem.bak)"
            )
        raw = stripped
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistrationError(
            f"{path} is not valid JSON ({exc}) — nothing was written. Fix the file (or move it "
            "aside) and re-run `sup-mem init`."
        ) from exc
    if not isinstance(data, dict):
        raise RegistrationError(
            f"{path} does not hold a JSON object — nothing was written. Fix the file (or move "
            "it aside) and re-run `sup-mem init`."
        )
    return data


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
def command_executable(command: Any) -> str:
    """The executable a hook command runs, past any ``env`` / ``VAR=value`` prefix and with
    shell quoting undone — ``env SUP_MEM_CLIENT=codex '/a b/sup-mem-hook-stop'`` →
    ``/a b/sup-mem-hook-stop``. ``""`` if there is none."""
    if not isinstance(command, str):
        return ""
    try:
        tokens = shlex.split(command)
    except ValueError:  # unbalanced quotes — fall back to plain whitespace splitting
        tokens = command.split()
    for token in tokens:
        if not (_ASSIGNMENT.match(token) or os.path.basename(token) == "env"):
            return token
    return ""


def _hook_command(script: str, env_client: str | None) -> str:
    """Resolve the console script. For non-default clients, tag the invocation with the client
    name so the hook picks the right stdout dialect + transcript parser (``SUP_MEM_CLIENT``).
    Claude Code gets the bare command (no tag) — its path stays byte-identical to before.
    Hosts run the string through a shell, so the path is shell-quoted (a no-op for ordinary
    paths) — an install path with spaces would otherwise split into broken words."""
    command = shlex.quote(_resolve_command(script))
    return f"env SUP_MEM_CLIENT={env_client} {command}" if env_client else command


def _merge_hooks_into(
    data: dict[str, Any],
    events: dict[str, str],
    env_client: str | None,
    *,
    timeout_ms: bool = False,
) -> bool:
    """Merge our hook entries into an in-memory Claude-Code-shaped ``hooks`` map. Returns changed?

    ``events`` maps canonical role -> this client's event name. Codex's ``hooks.json`` and
    Gemini's ``settings.json`` use the identical schema, so this one merger serves all three.
    ``timeout_ms`` writes ``TIMEOUTS`` in milliseconds (Gemini) instead of seconds.
    Pure (no I/O) so callers writing several sections into one file can do a single write.

    An existing hook is ours when the script it runs has our script's basename, whatever its
    directory (a project ``.venv`` vs the ``~/.local/bin`` shim) or env prefix. It is updated
    in place and duplicates of it are dropped, so a moved install never injects twice or
    leaves a stale entry failing every turn.
    """
    if not isinstance(data.get("hooks"), dict):
        data["hooks"] = {}
    hooks = data["hooks"]
    changed = False
    for role, event in events.items():
        script = SCRIPTS[role]
        desired: dict[str, Any] = {"type": "command", "command": _hook_command(script, env_client)}
        if role in TIMEOUTS:
            desired["timeout"] = TIMEOUTS[role] * 1000 if timeout_ms else TIMEOUTS[role]
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        ours = [
            (entry, hook)
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
            for hook in entry["hooks"]
            if isinstance(hook, dict)
            and os.path.basename(command_executable(hook.get("command"))) == script
        ]
        if not ours:
            entries.append({"hooks": [desired]})
            changed = True
            continue
        keep = ours[0][1]
        if os.sep not in command_executable(desired["command"]) and os.path.isfile(
            command_executable(keep.get("command"))
        ):  # ours didn't resolve on PATH right now — never trade a working path for a bare name
            desired["command"] = keep["command"]
        if any(keep.get(key) != value for key, value in desired.items()):
            keep.update(desired)  # in place — any extra keys the user set on it survive
            changed = True
        for entry, dupe in ours[1:]:  # left behind by older, path-sensitive registrations
            entry["hooks"] = [h for h in entry["hooks"] if h is not dupe]
            if not entry["hooks"]:
                entries[:] = [e for e in entries if e is not entry]
            changed = True
    return changed


# --------------------------------------------------------------------------------------------
# MCP server → ~/.claude.json (via `claude mcp add`, or a direct merge fallback)
# --------------------------------------------------------------------------------------------
def _mcp_command() -> str:
    return _resolve_command(MCP_SERVER_NAME)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True)


def _user_mcp_entry(claude_json: Path) -> dict[str, Any] | None:
    """Our current user-scope definition in ``~/.claude.json`` (what the CLI's user scope
    writes), or None if absent/unreadable."""
    try:
        servers = _load_json(claude_json).get("mcpServers")
    except RegistrationError:
        return None
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _register_mcp_via_cli(command: str, claude_json: Path, warnings: list[str]) -> bool | None:
    """Register with `claude mcp add --scope user`. Returns True/False if handled (changed?),
    or None if the `claude` binary isn't available (caller should fall back).

    Never leaves the user with less than they had: ``add`` is tried first, and a stale entry is
    removed only when ``add`` conflicts with it — with its definition snapshotted beforehand
    and put back (``add-json``, else a direct merge) if the re-add still fails.
    """
    claude = shutil.which("claude")
    if not claude:
        return None
    existing = _run([claude, "mcp", "get", MCP_SERVER_NAME])
    if existing.returncode == 0 and command in existing.stdout:
        return False  # already registered with our command → idempotent
    add = [claude, "mcp", "add", MCP_SERVER_NAME, "--scope", "user", "--", command, "serve"]
    added = _run(add)
    if added.returncode == 0:
        return True
    prior = _user_mcp_entry(claude_json) if existing.returncode == 0 else None
    if prior is not None:  # add conflicted with our stale user-scope entry → replace it
        _run([claude, "mcp", "remove", MCP_SERVER_NAME, "-s", "user"])
        added = _run(add)
        if added.returncode == 0:
            return True
        restore = [claude, "mcp", "add-json", MCP_SERVER_NAME, json.dumps(prior), "-s", "user"]
        if _run(restore).returncode != 0:  # CLI can't restore it either → write it back
            data = _load_json(claude_json)
            if not isinstance(data.get("mcpServers"), dict):
                data["mcpServers"] = {}
            data["mcpServers"][MCP_SERVER_NAME] = prior
            _atomic_write_json(claude_json, data)
    reason = (added.stderr or added.stdout).strip() or f"exit {added.returncode}"
    kept = " — the previous sup-mem MCP entry was kept" if existing.returncode == 0 else ""
    warnings.append(f"`claude mcp add` failed ({reason}){kept}")
    return False


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
    """Merge our hooks + MCP server into Claude Code config. Returns a report of what changed.

    Raises ``RegistrationError`` — before writing anything — if a file it would rewrite does
    not parse.
    """
    directory = claude_config_dir(claude_dir)
    directory.mkdir(parents=True, exist_ok=True)
    settings_path = directory / "settings.json"
    mcp_json = claude_json if claude_json is not None else Path.home() / ".claude.json"
    settings = _load_json(settings_path)
    if not use_cli or shutil.which("claude") is None:
        _load_json(mcp_json)  # the fallback merge rewrites it — validate before any write
    hooks_changed = _merge_hooks_into(settings, CLAUDE_EVENTS, None)
    if hooks_changed:
        _atomic_write_json(settings_path, settings)

    command = _mcp_command()
    warnings = _venv_warnings(command)
    cli_result = _register_mcp_via_cli(command, mcp_json, warnings) if use_cli else None
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
        "warnings": warnings,
    }


# --------------------------------------------------------------------------------------------
# Codex CLI  →  ~/.codex/hooks.json (hooks) + ~/.codex/config.toml (MCP)
# --------------------------------------------------------------------------------------------
def _load_toml(path: Path) -> dict[str, Any]:
    """Parse Codex's config.toml; missing → ``{}``, unparseable → ``RegistrationError``."""
    import tomllib

    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistrationError(
            f"{path} is not valid TOML ({exc}) — nothing was written. Fix the file (or move it "
            "aside) and re-run `sup-mem init`."
        ) from exc


def _toml_string(value: str) -> str:
    """A TOML basic string: quotes, backslashes and control chars escaped (paths may hold any)."""
    out = []
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _append_codex_mcp(
    config_toml: Path, command: str, existing: dict[str, Any]
) -> tuple[bool, str]:
    """Append an ``[mcp_servers.sup-mem]`` table to Codex's config.toml if absent.

    Append-only + a ``.sup-mem.bak`` — never rewrites the user's existing TOML (no stdlib TOML
    writer to round-trip it safely). A fresh top-level table at EOF is unambiguous.
    ``existing`` is the already-parsed file (``_load_toml``), so a bad file never gets here.
    """
    servers = existing.get("mcp_servers")
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        return False, str(config_toml)  # already registered → idempotent
    block = (
        f'\n[mcp_servers.{MCP_SERVER_NAME}]\ncommand = {_toml_string(command)}\nargs = ["serve"]\n'
    )
    config_toml.parent.mkdir(parents=True, exist_ok=True)
    if config_toml.exists():
        shutil.copy2(config_toml, config_toml.with_name(config_toml.name + ".sup-mem.bak"))
    with config_toml.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return True, str(config_toml)


def register_into_codex(config: Config, *, codex_home: Path | None = None) -> dict[str, Any]:
    """Merge our hooks (``hooks.json``) + MCP server (``config.toml``) into Codex CLI config.
    Codex speaks Claude's exact hook dialect, so the same scripts/JSON envelope just work.
    Both files are parsed before either is written (``RegistrationError`` → nothing touched)."""
    home = codex_home if codex_home is not None else Path.home() / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    hooks_path = home / "hooks.json"
    config_toml = home / "config.toml"
    hooks = _load_json(hooks_path)
    existing_toml = _load_toml(config_toml)
    hooks_changed = _merge_hooks_into(hooks, CODEX_EVENTS, "codex")
    if hooks_changed:
        _atomic_write_json(hooks_path, hooks)
    command = _mcp_command()
    mcp_changed, mcp_target = _append_codex_mcp(config_toml, command, existing_toml)
    return {
        "client": "codex",
        "config_dir": str(home),
        "settings_path": str(hooks_path),
        "mcp_target": mcp_target,
        "hooks_changed": hooks_changed,
        "mcp_changed": mcp_changed,
        "mcp_command": command,
        "warnings": _venv_warnings(command),
    }


# --------------------------------------------------------------------------------------------
# Gemini CLI  →  ~/.gemini/settings.json (hooks + MCP, both in one JSON file)
# --------------------------------------------------------------------------------------------
def register_into_gemini(config: Config, *, gemini_home: Path | None = None) -> dict[str, Any]:
    """Merge our hooks + MCP server into Gemini CLI's settings.json (same JSON schema as Claude;
    event names are renamed via ``GEMINI_EVENTS`` and the hook emits the JSON-envelope dialect).

    The file is JSONC to Gemini, so comments are stripped before parsing; hook timeouts are
    written in milliseconds (Gemini's unit)."""
    home = gemini_home if gemini_home is not None else Path.home() / ".gemini"
    home.mkdir(parents=True, exist_ok=True)
    settings_path = home / "settings.json"
    # Hooks AND mcpServers both live in this one file — merge both in memory, then write ONCE,
    # so the single ``.bak`` is the pristine original (two writes would clobber the first backup).
    notes: list[str] = []
    data = _load_json(settings_path, jsonc=True, notes=notes)
    hooks_changed = _merge_hooks_into(data, GEMINI_EVENTS, "gemini", timeout_ms=True)
    command = _mcp_command()
    mcp_changed = _merge_mcp_into(data, command)
    warnings = _venv_warnings(command)
    if hooks_changed or mcp_changed:
        _atomic_write_json(settings_path, data)
        warnings += notes  # the rewrite dropped the user's comments — say so
    return {
        "client": "gemini",
        "config_dir": str(home),
        "settings_path": str(settings_path),
        "mcp_target": str(settings_path),
        "hooks_changed": hooks_changed,
        "mcp_changed": mcp_changed,
        "mcp_command": command,
        "warnings": warnings,
    }
