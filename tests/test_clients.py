"""Multi-client support: registration adapters, transcript parsers, injection dialect.

All hermetic — Codex/Gemini registration is pointed at tmp *_home dirs; nothing touches the
developer's real ~/.codex / ~/.gemini / ~/.claude.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import tomllib
from pathlib import Path

import pytest

from sup_mem import commands
from sup_mem.clients import active_client_name, detect_installed, get_client
from sup_mem.config import Config
from sup_mem.hook.emit import emit_context
from sup_mem.registration import (
    MCP_SERVER_NAME,
    SCRIPTS,
    RegistrationError,
    command_executable,
    register_into_codex,
    register_into_gemini,
)


def _fake_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bin_dir: Path) -> Path:
    """Fake executables for every sup-mem script in ``bin_dir``; HOME/PATH point at tmp only."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in [*SCRIPTS.values(), MCP_SERVER_NAME]:
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


# --- client selection ----------------------------------------------------------------------
def test_active_client_defaults_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUP_MEM_CLIENT", raising=False)
    assert active_client_name() == "claude"
    monkeypatch.setenv("SUP_MEM_CLIENT", "codex")
    assert active_client_name() == "codex"
    monkeypatch.setenv("SUP_MEM_CLIENT", "nonsense")  # unknown → safe default
    assert active_client_name() == "claude"


def test_get_client_falls_back_to_claude() -> None:
    assert get_client("gemini").name == "gemini"
    assert get_client("who-dis").name == "claude"


# --- injection dialect ---------------------------------------------------------------------
def test_emit_plain_for_claude(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SUP_MEM_CLIENT", raising=False)
    emit_context("remember the deploy is blue-green")
    assert capsys.readouterr().out.strip() == "remember the deploy is blue-green"


def test_emit_json_envelope_for_gemini(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SUP_MEM_CLIENT", "gemini")
    emit_context("the postgres pool is capped at fifty")
    payload = json.loads(capsys.readouterr().out)
    assert (
        payload["hookSpecificOutput"]["additionalContext"] == "the postgres pool is capped at fifty"
    )


def test_emit_nothing_stays_silent(capsys: pytest.CaptureFixture[str]) -> None:
    emit_context("")
    assert capsys.readouterr().out == ""


# --- Codex registration --------------------------------------------------------------------
def test_codex_registration_writes_hooks_and_appends_mcp(config: Config, tmp_path: Path) -> None:
    home = tmp_path / "codex"
    report = register_into_codex(config, codex_home=home)
    assert report["client"] == "codex" and report["hooks_changed"] and report["mcp_changed"]

    hooks = json.loads((home / "hooks.json").read_text())["hooks"]
    # Codex shares Claude's exact event vocabulary.
    for event in ("UserPromptSubmit", "SessionStart", "Stop", "PreCompact"):
        assert event in hooks
    cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "SUP_MEM_CLIENT=codex" in cmd and "sup-mem-hook-userprompt" in cmd

    toml_text = (home / "config.toml").read_text()
    assert "[mcp_servers.sup-mem]" in toml_text
    parsed = tomllib.loads(toml_text)
    assert parsed["mcp_servers"]["sup-mem"]["args"] == ["serve"]


def test_codex_registration_is_idempotent_and_non_clobbering(
    config: Config, tmp_path: Path
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    # A pre-existing config.toml with the user's own content + a different MCP server.
    (home / "config.toml").write_text(
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8"
    )
    first = register_into_codex(config, codex_home=home)
    assert first["mcp_changed"]

    parsed = tomllib.loads((home / "config.toml").read_text())
    assert parsed["model"] == "gpt-5"  # untouched
    assert "other" in parsed["mcp_servers"] and "sup-mem" in parsed["mcp_servers"]
    assert (home / "config.toml.sup-mem.bak").exists()

    second = register_into_codex(config, codex_home=home)
    assert not second["hooks_changed"] and not second["mcp_changed"]  # nothing re-added
    assert (home / "config.toml").read_text().count("[mcp_servers.sup-mem]") == 1


# --- Gemini registration -------------------------------------------------------------------
def test_gemini_registration_uses_renamed_events_and_envelope_client(
    config: Config, tmp_path: Path
) -> None:
    home = tmp_path / "gemini"
    # Pre-existing settings.json with an unrelated key + MCP server, to prove non-clobbering.
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "other"}}}),
        encoding="utf-8",
    )
    report = register_into_gemini(config, gemini_home=home)
    assert report["client"] == "gemini" and report["hooks_changed"] and report["mcp_changed"]

    data = json.loads((home / "settings.json").read_text())
    assert data["theme"] == "dark"  # untouched
    for event in ("BeforeAgent", "SessionStart", "AfterAgent", "PreCompress"):
        assert event in data["hooks"]
    cmd = data["hooks"]["BeforeAgent"][0]["hooks"][0]["command"]
    assert "SUP_MEM_CLIENT=gemini" in cmd and "sup-mem-hook-userprompt" in cmd
    assert "other" in data["mcpServers"] and data["mcpServers"]["sup-mem"]["args"] == ["serve"]


def test_gemini_backup_is_the_pristine_original(config: Config, tmp_path: Path) -> None:
    # Gemini writes hooks AND mcpServers to one file. A pre-existing settings.json (here with the
    # user's own BeforeTool hook, mirroring a real machine) must be backed up *pristine* — one
    # write, not two (two would leave the .bak holding our own first-write edits).
    home = tmp_path / "gemini"
    home.mkdir()
    original = json.dumps(
        {
            "theme": "dark",
            "hooks": {"BeforeTool": [{"hooks": [{"type": "command", "command": "user-own"}]}]},
            "mcpServers": {"other": {"command": "other"}},
        },
        indent=2,
    )
    settings = home / "settings.json"
    settings.write_text(original, encoding="utf-8")
    pristine = hashlib.sha256(settings.read_bytes()).hexdigest()

    register_into_gemini(config, gemini_home=home)

    bak = home / "settings.json.sup-mem.bak"
    assert bak.exists()
    assert hashlib.sha256(bak.read_bytes()).hexdigest() == pristine  # exact original, restorable
    data = json.loads(settings.read_text())  # live file has user's hook + ours, both MCP servers
    assert {"BeforeTool", "BeforeAgent", "AfterAgent"} <= set(data["hooks"])
    assert {"other", "sup-mem"} <= set(data["mcpServers"])


def test_gemini_jsonc_settings_are_parsed_not_wiped(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gemini's settings.json is JSONC. Comments (outside strings) must not make us treat the
    # file as empty — that wiped everything but our keys. Comment-like text INSIDE strings
    # must survive untouched.
    _fake_path(monkeypatch, tmp_path, tmp_path / "bin")
    home = tmp_path / "gemini"
    home.mkdir()
    original = (
        "// my gemini settings\n"
        "{\n"
        '  "theme": "dark", // inline note\n'
        "  /* block\n     comment */\n"
        '  "proxy": "http://proxy.local:8080",\n'
        '  "banner": "a /* not a comment */ b // nor this",\n'
        '  "quote": "she said \\"hi\\" // still a string",\n'
        '  "mcpServers": {"other": {"command": "other"}}\n'
        "}\n"
    )
    settings = home / "settings.json"
    settings.write_text(original, encoding="utf-8")

    report = register_into_gemini(config, gemini_home=home)
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["proxy"] == "http://proxy.local:8080"
    assert data["banner"] == "a /* not a comment */ b // nor this"
    assert data["quote"] == 'she said "hi" // still a string'
    assert {"other", "sup-mem"} <= set(data["mcpServers"])
    assert (home / "settings.json.sup-mem.bak").read_text(encoding="utf-8") == original
    assert any("comments" in w and "sup-mem.bak" in w for w in report["warnings"])


def test_gemini_unparseable_settings_abort_without_writing(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_path(monkeypatch, tmp_path, tmp_path / "bin")
    home = tmp_path / "gemini"
    home.mkdir()
    original = '// still broken after comments go\n{"theme": "dark",}\n'
    (home / "settings.json").write_text(original, encoding="utf-8")
    with pytest.raises(RegistrationError, match="settings.json"):
        register_into_gemini(config, gemini_home=home)
    assert (home / "settings.json").read_text(encoding="utf-8") == original
    assert not (home / "settings.json.sup-mem.bak").exists()


def test_gemini_hook_timeout_is_milliseconds_and_stale_entry_is_fixed(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gemini reads hook timeouts in MILLISECONDS; a pre-fix registration wrote 120 (= 120 ms)
    # from an old venv path. Re-registering must fix that entry in place, not add a second.
    bin_dir = _fake_path(monkeypatch, tmp_path, tmp_path / "bin")
    home = tmp_path / "gemini"
    home.mkdir()
    stale = {
        "type": "command",
        "command": "env SUP_MEM_CLIENT=gemini /old/.venv/bin/sup-mem-hook-precompact",
        "timeout": 120,
    }
    (home / "settings.json").write_text(
        json.dumps({"hooks": {"PreCompress": [{"hooks": [stale]}]}}), encoding="utf-8"
    )
    register_into_gemini(config, gemini_home=home)
    data = json.loads((home / "settings.json").read_text())
    hooks = [h for e in data["hooks"]["PreCompress"] for h in e["hooks"]]
    assert len(hooks) == 1
    assert hooks[0]["timeout"] == 120_000
    assert command_executable(hooks[0]["command"]) == str(bin_dir / "sup-mem-hook-precompact")


# --- Codex: unparseable config + TOML/shell escaping ----------------------------------------
def test_codex_unparseable_config_toml_aborts_before_any_write(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_path(monkeypatch, tmp_path, tmp_path / "bin")
    home = tmp_path / "codex"
    home.mkdir()
    original = 'model = "gpt-5"\n[mcp_servers.other\n'
    (home / "config.toml").write_text(original, encoding="utf-8")
    with pytest.raises(RegistrationError, match="config.toml"):
        register_into_codex(config, codex_home=home)
    assert (home / "config.toml").read_text(encoding="utf-8") == original
    assert not (home / "hooks.json").exists()  # no half-registration


def test_codex_unparseable_hooks_json_aborts(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_path(monkeypatch, tmp_path, tmp_path / "bin")
    home = tmp_path / "codex"
    home.mkdir()
    (home / "hooks.json").write_text('{"hooks": {,}}', encoding="utf-8")
    (home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    with pytest.raises(RegistrationError, match="hooks.json"):
        register_into_codex(config, codex_home=home)
    assert (home / "hooks.json").read_text(encoding="utf-8") == '{"hooks": {,}}'
    assert (home / "config.toml").read_text(encoding="utf-8") == 'model = "gpt-5"\n'


def test_codex_awkward_install_path_is_escaped(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Spaces, a double quote and a backslash: the shell command must quote it, the TOML
    # string must escape it — both must round-trip to the exact path.
    bin_dir = _fake_path(monkeypatch, tmp_path, tmp_path / 'we "ird\\dir' / "bin")
    home = tmp_path / "codex"
    register_into_codex(config, codex_home=home)

    parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["sup-mem"]["command"] == str(bin_dir / "sup-mem")

    hooks = json.loads((home / "hooks.json").read_text())["hooks"]
    cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert shlex.split(cmd) == [
        "env",
        "SUP_MEM_CLIENT=codex",
        str(bin_dir / "sup-mem-hook-userprompt"),
    ]
    assert hooks["PreCompact"][0]["hooks"][0]["timeout"] == 120  # Codex: seconds


# --- transcript parsers --------------------------------------------------------------------
def test_codex_transcript_parser_reads_rollout_jsonl(tmp_path: Path) -> None:
    # Real Codex rollout shape: {type:"response_item", payload:{type:"message", role, content}}.
    def rec(role: str, text: str, btype: str) -> str:
        return json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": btype, "text": text}],
                },
            }
        )

    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {"id": "x"}}),  # skipped
                rec("user", "how does deploy work", "input_text"),
                json.dumps({"type": "response_item", "payload": {"type": "reasoning"}}),  # skipped
                rec("assistant", "blue-green", "output_text"),
                rec("developer", "system instructions", "input_text"),  # skipped (not user/asst)
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_image"},  # dropped (no text)
                                {"type": "input_text", "text": "and the db pool?"},
                            ],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    turns = get_client("codex").parse_transcript(path)
    assert [t.role for t in turns] == ["user", "assistant", "user"]
    assert turns[1].text == "blue-green"
    assert turns[2].text == "and the db pool?"  # image block dropped, text kept


def test_gemini_transcript_parser_reads_object_with_messages(tmp_path: Path) -> None:
    # Real Gemini CLI chat shape: {messages:[{type:"user"|"gemini", content:"..."}]}.
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "sessionId": "abc",
                "messages": [
                    {"type": "user", "content": "q1"},
                    {"type": "gemini", "content": "a1"},
                    {"type": "info", "content": "skip me"},
                    {"type": "error", "content": "skip me too"},
                ],
            }
        ),
        encoding="utf-8",
    )
    turns = get_client("gemini").parse_transcript(path)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].text == "q1" and turns[1].text == "a1"


def test_gemini_transcript_parser_falls_back_to_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "first"}),
                json.dumps({"role": "assistant", "content": "second"}),
            ]
        ),
        encoding="utf-8",
    )
    turns = get_client("gemini").parse_transcript(path)
    assert [t.text for t in turns] == ["first", "second"]


def test_parsers_fail_open_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert get_client("codex").parse_transcript(missing) == []
    assert get_client("gemini").parse_transcript(missing) == []


# --- init dispatch (single non-default client, hermetic) -----------------------------------
def test_init_client_codex_wires_only_codex(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    codex_home = tmp_path / "codex"
    rc = commands.cmd_init(config, clients=["codex"], codex_home=codex_home)
    assert rc == 0
    assert (codex_home / "hooks.json").exists() and (codex_home / "config.toml").exists()
    assert not (tmp_path / "claude").exists()  # Claude was not selected


def test_detect_installed_returns_known_names_only() -> None:
    # Whatever the host has, detection must only ever report names we can actually wire.
    assert set(detect_installed()) <= {"claude", "codex", "gemini"}
