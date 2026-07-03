"""Multi-client support: registration adapters, transcript parsers, injection dialect.

All hermetic — Codex/Gemini registration is pointed at tmp *_home dirs; nothing touches the
developer's real ~/.codex / ~/.gemini / ~/.claude.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from sup_mem import commands
from sup_mem.clients import active_client_name, detect_installed, get_client
from sup_mem.config import Config
from sup_mem.hook.emit import emit_context
from sup_mem.registration import (
    register_into_antigravity,
    register_into_codex,
    register_into_gemini,
)


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


def test_antigravity_transcript_parser_excludes_tool_output(tmp_path: Path) -> None:
    # Real Antigravity brain/**/transcript.jsonl steps {type, source, content}: user = USER_INPUT
    # (USER_EXPLICIT); assistant = MODEL prose (PLANNER_RESPONSE/GENERIC). Tool-output steps
    # (VIEW_FILE/GREP_SEARCH/…) carry tool I/O, NOT the assistant's words — must be excluded.
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "how does deploy work",
                },
                {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "use blue-green"},
                {
                    "type": "VIEW_FILE",
                    "source": "MODEL",
                    "content": "SECRET file body — exclude me",
                },
                {"type": "GREP_SEARCH", "source": "MODEL", "content": "grep hits — exclude me"},
                {"type": "GENERIC", "source": "MODEL", "content": "done"},
                {"type": "CONVERSATION_HISTORY", "source": "SYSTEM", "content": ""},
                {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "and the db?"},
            ]
        ),
        encoding="utf-8",
    )
    turns = get_client("antigravity").parse_transcript(path)
    assert [t.role for t in turns] == ["user", "assistant", "assistant", "user"]
    assert turns[1].text == "use blue-green"
    blob = " ".join(t.text for t in turns)
    assert "SECRET file body" not in blob and "grep hits" not in blob  # tool I/O excluded


def test_antigravity_registration_is_nonclobbering_and_idempotent(
    config: Config, tmp_path: Path
) -> None:
    # hooks.json is name-keyed ({name:{Event:[handler]}}); MCP is a standard mcpServers file.
    hooks = tmp_path / "config" / "hooks.json"
    mcp = tmp_path / "antigravity" / "mcp_config.json"
    hooks.parent.mkdir(parents=True)
    mcp.parent.mkdir(parents=True)
    hooks.write_text(
        json.dumps({"user-linter": {"PreToolUse": [{"matcher": "x", "hooks": []}]}}),
        encoding="utf-8",
    )
    mcp.write_text(
        json.dumps({"mcpServers": {"codebase-memory-mcp": {"command": "cbm"}}}), encoding="utf-8"
    )

    report = register_into_antigravity(config, hooks_path=hooks, mcp_config_path=mcp)
    assert report["hooks_changed"] and report["mcp_changed"]

    hj = json.loads(hooks.read_text())
    assert "user-linter" in hj  # pre-existing hook preserved
    assert set(hj["sup-mem"]) == {"PreInvocation", "Stop"}
    cmd = hj["sup-mem"]["PreInvocation"][0]["command"]
    assert "SUP_MEM_CLIENT=antigravity" in cmd and "sup-mem-hook-userprompt" in cmd

    mj = json.loads(mcp.read_text())
    assert "codebase-memory-mcp" in mj["mcpServers"]  # pre-existing MCP server preserved
    assert mj["mcpServers"]["sup-mem"]["args"] == ["serve"]

    again = register_into_antigravity(config, hooks_path=hooks, mcp_config_path=mcp)
    assert not again["hooks_changed"] and not again["mcp_changed"]  # idempotent


def test_emit_context_antigravity_uses_json_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SUP_MEM_CLIENT", "antigravity")
    emit_context("hello")
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == "hello"


def test_parsers_fail_open_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert get_client("codex").parse_transcript(missing) == []
    assert get_client("gemini").parse_transcript(missing) == []
    assert get_client("antigravity").parse_transcript(missing) == []


# --- init dispatch (single non-default client, hermetic) -----------------------------------
def test_init_client_codex_wires_only_codex(
    config: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    codex_home = tmp_path / "codex"
    rc = commands.cmd_init(config, clients=["codex"], codex_home=codex_home)
    assert rc == 0
    assert (codex_home / "hooks.json").exists() and (codex_home / "config.toml").exists()
    assert not (tmp_path / "claude").exists()  # Claude was not selected


def test_init_client_antigravity_wires_hooks_and_mcp(config: Config, tmp_path: Path) -> None:
    hooks = tmp_path / "ag-config" / "hooks.json"
    mcp = tmp_path / "ag" / "mcp_config.json"
    rc = commands.cmd_init(
        config, clients=["antigravity"], antigravity_hooks=hooks, antigravity_mcp=mcp
    )
    assert rc == 0
    assert hooks.exists() and mcp.exists()
    assert "sup-mem" in json.loads(hooks.read_text())


def test_detect_installed_returns_known_names_only() -> None:
    # Whatever the host has, detection must only ever report names we can actually wire.
    assert set(detect_installed()) <= {"claude", "codex", "gemini", "antigravity"}
