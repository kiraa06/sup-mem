"""End-to-end smoke: init → remember (MCP front-door) → hook injects it (HANDOVER §11 / §4).

Exercises the two front-doors over one backend (I1) plus the tiered hook, on the default
SQLite FTS path (no optional deps, no services).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from claude_memory import commands
from claude_memory.config import load_config
from claude_memory.hook import user_prompt_submit as hook
from claude_memory.mcp.server import MemoryTools


def test_init_remember_then_hook_retrieves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    claude_dir = tmp_path / "claude"
    cfg = load_config(overrides={"data_dir": str(data_dir)})

    # 1) init the default store + register with Claude Code (scratch config dir).
    assert commands.cmd_init(cfg, claude_dir=claude_dir) == 0
    assert (data_dir / "memory.db").exists()
    capsys.readouterr()  # flush init's console output

    # 2) store a durable memory via the MCP `remember` tool (front-door B).
    tools = MemoryTools(cfg)
    try:
        msg = tools.remember(
            "The staging deploy uses a blue-green strategy driven by the deploy.sh script.",
            tags=["deploy", "staging"],
        )
        assert "stored" in msg.lower()
    finally:
        tools.close()

    # 3) the per-prompt hook (front-door A) injects it for a relevant prompt.
    monkeypatch.setenv("CLAUDE_MEMORY_DATA_DIR", str(data_dir))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"prompt": "how does our staging deploy strategy use deploy.sh?"})),
    )
    assert hook.main() == 0
    out = capsys.readouterr().out
    assert "blue-green" in out
    assert "auto-retrieved" in out

    # retrieval log written for tuning (§8, on by default).
    log_path = data_dir / "retrieval.jsonl"
    assert log_path.exists()
    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert any(e["tier"] == "retrieve" for e in entries)

    # 4) trivial turn is skipped — nothing beyond Tier 0 injected.
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "thanks, perfect!"})))
    assert hook.main() == 0
    assert "auto-retrieved" not in capsys.readouterr().out
