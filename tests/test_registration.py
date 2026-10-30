"""CLI init + non-clobbering Claude Code registration (HANDOVER §7).

``use_cli=False`` forces the deterministic file-merge path so tests never shell out to the
real ``claude`` binary (which would touch the developer's ~/.claude.json).
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sup_mem import commands, registration
from sup_mem.config import Config, load_config
from sup_mem.registration import (
    MCP_SERVER_NAME,
    SCRIPTS,
    RegistrationError,
    command_executable,
    register_into_claude_code,
)
from sup_mem.status import collect_checks


def test_init_creates_store_config_pinned_and_registers(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    claude_dir = tmp_path / "claude"
    claude_json = claude_dir / ".claude.json"
    cfg = load_config(overrides={"data_dir": str(data_dir)})

    rc = commands.cmd_init(
        cfg, clients=["claude"], claude_dir=claude_dir, claude_json=claude_json, use_cli=False
    )
    assert rc == 0

    assert (data_dir / "memory.db").exists()
    assert (data_dir / "config.toml").exists()
    assert (data_dir / "pinned.md").exists()

    settings = json.loads((claude_dir / "settings.json").read_text())
    assert "UserPromptSubmit" in settings["hooks"]
    assert "SessionStart" in settings["hooks"]

    mcp = json.loads(claude_json.read_text())
    assert mcp["mcpServers"]["sup-mem"]["args"] == ["serve"]


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
    assert any("sup-mem-hook-userprompt" in c for c in ups)  # ours added alongside

    mcp = json.loads(claude_json.read_text())
    assert mcp["numStartups"] == 42  # unrelated top-level key preserved
    assert "codegraph" in mcp["mcpServers"] and "sup-mem" in mcp["mcpServers"]

    # Backups of the pre-existing files were kept.
    assert (claude_dir / "settings.json.sup-mem.bak").exists()
    assert claude_json.with_name(".claude.json.sup-mem.bak").exists()

    # Re-running changes nothing (idempotent).
    second = register_into_claude_code(
        cfg, claude_dir=claude_dir, claude_json=claude_json, use_cli=False
    )
    assert not second["hooks_changed"] and not second["mcp_changed"]


# --- hardening: parse failures, moved installs, quoting, timeouts, MCP CLI safety ----------
# Every test below fakes HOME and PATH (fake executables in tmp dirs), so script resolution is
# deterministic and nothing reads or writes the developer's real ~/.claude or ~/.local/bin.
ALL_SCRIPTS = [*SCRIPTS.values(), MCP_SERVER_NAME]


def _fake_bin(directory: Path, names: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        exe = directory / name
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
    return directory


def _fake_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bin_dir: Path) -> Path:
    """Point HOME at a fresh tmp dir and PATH at ``bin_dir`` only. Returns the fake HOME."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    return home


def _ours(settings: dict[str, Any], event: str, script: str) -> list[dict[str, Any]]:
    return [
        hook
        for entry in settings["hooks"].get(event, [])
        for hook in entry["hooks"]
        if script in hook["command"]
    ]


def test_unparseable_settings_aborts_without_writing(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_env(monkeypatch, tmp_path, _fake_bin(tmp_path / "bin", ALL_SCRIPTS))
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    original = '{\n  "model": "opus",\n  "permissions": {"allow": ["Bash"]},\n}\n'  # trailing ,
    settings.write_text(original, encoding="utf-8")
    claude_json = tmp_path / ".claude.json"

    with pytest.raises(RegistrationError, match="settings.json") as err:
        register_into_claude_code(config, claude_dir=claude_dir, claude_json=claude_json)
    assert "nothing was written" in str(err.value)
    assert settings.read_text(encoding="utf-8") == original  # the user's settings survive
    assert not (claude_dir / "settings.json.sup-mem.bak").exists()
    assert not claude_json.exists()  # the MCP half was never attempted


def test_unparseable_claude_json_aborts_before_hooks_are_written(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_env(monkeypatch, tmp_path, _fake_bin(tmp_path / "bin", ALL_SCRIPTS))
    claude_dir = tmp_path / "claude"
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text('{"numStartups": 3,,}', encoding="utf-8")
    with pytest.raises(RegistrationError, match=r"\.claude\.json"):
        register_into_claude_code(
            config, claude_dir=claude_dir, claude_json=claude_json, use_cli=False
        )
    assert not (claude_dir / "settings.json").exists()  # no half-registration
    assert claude_json.read_text(encoding="utf-8") == '{"numStartups": 3,,}'


def test_non_object_json_is_refused_and_blank_is_empty(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_env(monkeypatch, tmp_path, _fake_bin(tmp_path / "bin", ALL_SCRIPTS))
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(RegistrationError, match="JSON object"):
        register_into_claude_code(
            config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
        )
    (claude_dir / "settings.json").write_text("  \n", encoding="utf-8")  # blank: nothing to lose
    report = register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    assert report["hooks_changed"]


def test_init_reports_unparseable_config_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    _fake_env(monkeypatch, tmp_path, _fake_bin(tmp_path / "bin", ALL_SCRIPTS))
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"model": "opus",}', encoding="utf-8")
    cfg = load_config(overrides={"data_dir": str(tmp_path / "data")})

    rc = commands.cmd_init(
        cfg,
        clients=["claude"],
        claude_dir=claude_dir,
        claude_json=tmp_path / ".claude.json",
        use_cli=False,
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "not registered" in out and "settings.json" in out
    assert (claude_dir / "settings.json").read_text(encoding="utf-8") == '{"model": "opus",}'


def test_moved_install_replaces_stale_entry_in_place(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _fake_bin(tmp_path / "bin", ALL_SCRIPTS)
    _fake_env(monkeypatch, tmp_path, bin_dir)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    stale = "/old/project/.venv/bin/sup-mem-hook-userprompt"
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "user-own-hook"}]},
                {"hooks": [{"type": "command", "command": stale, "name": "kept-key"}]},
                {"hooks": [{"type": "command", "command": "sup-mem-hook-userprompt"}]},  # dupe
            ],
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    report = register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    assert report["hooks_changed"]
    merged = json.loads((claude_dir / "settings.json").read_text())
    ours = _ours(merged, "UserPromptSubmit", "sup-mem-hook-userprompt")
    assert len(ours) == 1  # replaced in place + duplicate dropped → never injects twice
    assert ours[0]["command"] == str(bin_dir / "sup-mem-hook-userprompt")
    assert ours[0]["name"] == "kept-key"  # updated in place, not re-created
    ups = [h["command"] for e in merged["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert "user-own-hook" in ups and len(merged["hooks"]["UserPromptSubmit"]) == 2

    again = register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    assert not again["hooks_changed"]  # idempotent


def test_unresolvable_script_never_downgrades_a_working_entry(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _fake_bin(tmp_path / "installed", ALL_SCRIPTS)
    _fake_env(monkeypatch, tmp_path, tmp_path / "empty-path")  # nothing resolvable on PATH
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    working = str(installed / "sup-mem-hook-precompact")
    settings = {"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": working}]}]}}
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    merged = json.loads((claude_dir / "settings.json").read_text())
    hooks = _ours(merged, "PreCompact", "sup-mem-hook-precompact")
    assert len(hooks) == 1 and hooks[0]["command"] == working  # kept, not made bare
    assert hooks[0]["timeout"] == 120  # the managed timeout is still applied


def test_wrapper_hook_is_not_mistaken_for_ours(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_env(monkeypatch, tmp_path, _fake_bin(tmp_path / "bin", ALL_SCRIPTS))
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    wrapper = {"type": "command", "command": "my-wrapper sup-mem-hook-userprompt"}
    settings = {"hooks": {"UserPromptSubmit": [{"hooks": [wrapper]}]}}
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    merged = json.loads((claude_dir / "settings.json").read_text())
    ups = [h["command"] for e in merged["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
    assert ups[0] == "my-wrapper sup-mem-hook-userprompt" and len(ups) == 2  # left alone


def test_venv_resolution_prefers_local_bin_shim(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_bin = _fake_bin(tmp_path / "proj" / ".venv" / "bin", ALL_SCRIPTS)
    home = _fake_env(monkeypatch, tmp_path, venv_bin)
    shim_dir = _fake_bin(home / ".local" / "bin", ALL_SCRIPTS)
    claude_dir = tmp_path / "claude"
    report = register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    merged = json.loads((claude_dir / "settings.json").read_text())
    assert _ours(merged, "Stop", "sup-mem-hook-stop")[0]["command"] == str(
        shim_dir / "sup-mem-hook-stop"
    )
    assert report["mcp_command"] == str(shim_dir / "sup-mem")
    assert report["warnings"] == []


def test_venv_resolution_without_shim_warns(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_bin = _fake_bin(tmp_path / "proj" / ".venv" / "bin", ALL_SCRIPTS)
    _fake_env(monkeypatch, tmp_path, venv_bin)
    report = register_into_claude_code(
        config, claude_dir=tmp_path / "claude", claude_json=tmp_path / "c.json", use_cli=False
    )
    assert report["mcp_command"] == str(venv_bin / "sup-mem")
    assert len(report["warnings"]) == 1  # one warning per venv dir, not per script
    assert ".venv" in report["warnings"][0] and "uv tool install" in report["warnings"][0]


def test_install_path_with_spaces_is_shell_quoted(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _fake_bin(tmp_path / "My Tools" / "bin", ALL_SCRIPTS)
    _fake_env(monkeypatch, tmp_path, bin_dir)
    claude_dir = tmp_path / "claude"
    claude_json = tmp_path / "c.json"
    register_into_claude_code(config, claude_dir=claude_dir, claude_json=claude_json, use_cli=False)
    merged = json.loads((claude_dir / "settings.json").read_text())
    cmd = _ours(merged, "UserPromptSubmit", "sup-mem-hook-userprompt")[0]["command"]
    target = str(bin_dir / "sup-mem-hook-userprompt")
    assert cmd == shlex.quote(target) and shlex.split(cmd) == [target]
    assert command_executable(cmd) == target
    # The MCP command is exec'd directly (argv, no shell) → stored raw, unquoted.
    mcp = json.loads(claude_json.read_text())["mcpServers"]["sup-mem"]
    assert mcp["command"] == str(bin_dir / "sup-mem")
    # `status` undoes the quoting when it checks that the hook binary exists.
    checks = {
        c.name: c for c in collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)
    }
    assert checks["hook:UserPromptSubmit"].ok, checks["hook:UserPromptSubmit"].detail


def test_claude_hook_timeout_stays_in_seconds(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_env(monkeypatch, tmp_path, _fake_bin(tmp_path / "bin", ALL_SCRIPTS))
    claude_dir = tmp_path / "claude"
    register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=tmp_path / "c.json", use_cli=False
    )
    merged = json.loads((claude_dir / "settings.json").read_text())
    assert _ours(merged, "PreCompact", "sup-mem-hook-precompact")[0]["timeout"] == 120


# --- `claude mcp` CLI path: a failed add must never leave the user with no registration ---
FakeRun = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
PRIOR = {"type": "stdio", "command": "/old/.venv/bin/sup-mem", "args": ["serve"], "env": {"A": "1"}}


def _fake_claude_cli(
    claude_json: Path, *, add_fails: bool = False, add_json_fails: bool = False
) -> tuple[FakeRun, list[list[str]]]:
    """Simulates `claude mcp get/add/remove/add-json` against the tmp ~/.claude.json."""
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        args = argv[1:]
        calls.append(args)
        data = json.loads(claude_json.read_text()) if claude_json.exists() else {}
        servers = data.setdefault("mcpServers", {})
        entry = servers.get("sup-mem")
        rc, out = 0, ""
        if args[:2] == ["mcp", "get"]:
            rc, out = (0, f"  Command: {entry['command']}\n") if entry else (1, "")
        elif args[:2] == ["mcp", "remove"]:
            rc = 0 if servers.pop("sup-mem", None) else 1
        elif args[:2] == ["mcp", "add"]:
            if entry is not None or add_fails:
                rc = 1  # name conflict / broken CLI
            else:
                cut = args.index("--")
                servers["sup-mem"] = {"command": args[cut + 1], "args": args[cut + 2 :]}
        elif args[:2] == ["mcp", "add-json"]:
            if add_json_fails:
                rc = 1
            else:
                servers["sup-mem"] = json.loads(args[3])
        claude_json.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")

    return run, calls


def _cli_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior: dict[str, Any] | None
) -> tuple[Path, Path]:
    bin_dir = _fake_bin(tmp_path / "bin", [*ALL_SCRIPTS, "claude"])
    _fake_env(monkeypatch, tmp_path, bin_dir)
    claude_json = tmp_path / ".claude.json"
    if prior is not None:
        claude_json.write_text(json.dumps({"mcpServers": {"sup-mem": prior}}), encoding="utf-8")
    return bin_dir, claude_json


def _cli_mcp(claude_json: Path) -> Any:
    return json.loads(claude_json.read_text())["mcpServers"]["sup-mem"]


def test_mcp_cli_add_without_existing_entry_never_removes(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, claude_json = _cli_setup(tmp_path, monkeypatch, None)
    fake, calls = _fake_claude_cli(claude_json)
    monkeypatch.setattr(registration, "_run", fake)
    report = register_into_claude_code(
        config, claude_dir=tmp_path / "claude", claude_json=claude_json
    )
    assert report["mcp_changed"]
    assert [c[1] for c in calls] == ["get", "add"]  # no remove when nothing conflicts
    assert _cli_mcp(claude_json)["command"] == str(bin_dir / "sup-mem")


def test_mcp_cli_conflicting_stale_entry_is_replaced(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, claude_json = _cli_setup(tmp_path, monkeypatch, PRIOR)
    fake, calls = _fake_claude_cli(claude_json)
    monkeypatch.setattr(registration, "_run", fake)
    report = register_into_claude_code(
        config, claude_dir=tmp_path / "claude", claude_json=claude_json
    )
    assert report["mcp_changed"] and report["warnings"] == []
    assert [c[1] for c in calls] == ["get", "add", "remove", "add"]  # remove only on conflict
    assert _cli_mcp(claude_json)["command"] == str(bin_dir / "sup-mem")


def test_mcp_cli_failed_readd_restores_previous_definition(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, claude_json = _cli_setup(tmp_path, monkeypatch, PRIOR)
    fake, calls = _fake_claude_cli(claude_json, add_fails=True)
    monkeypatch.setattr(registration, "_run", fake)
    report = register_into_claude_code(
        config, claude_dir=tmp_path / "claude", claude_json=claude_json
    )
    assert not report["mcp_changed"]
    assert [c[1] for c in calls] == ["get", "add", "remove", "add", "add-json"]
    assert _cli_mcp(claude_json) == PRIOR  # the previous definition, exactly
    assert any("previous sup-mem MCP entry was kept" in w for w in report["warnings"])


def test_mcp_cli_restore_falls_back_to_direct_merge(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, claude_json = _cli_setup(tmp_path, monkeypatch, PRIOR)
    fake, _ = _fake_claude_cli(claude_json, add_fails=True, add_json_fails=True)
    monkeypatch.setattr(registration, "_run", fake)
    register_into_claude_code(config, claude_dir=tmp_path / "claude", claude_json=claude_json)
    assert _cli_mcp(claude_json) == PRIOR
