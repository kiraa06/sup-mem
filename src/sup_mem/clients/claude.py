"""Claude Code adapter — the reference host (HANDOVER §6/§7). Delegates registration and
transcript parsing to the original, battle-tested implementations so this path is unchanged."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sup_mem.clients.base import ClientAdapter
from sup_mem.registration import CLAUDE_EVENTS

if TYPE_CHECKING:
    from sup_mem.config import Config
    from sup_mem.ledger import Turn


class ClaudeClient(ClientAdapter):
    name = "claude"
    events = CLAUDE_EVENTS
    dialect = "plain"  # Claude Code injects raw stdout into context

    def is_installed(self) -> bool:
        return (Path.home() / ".claude").exists() or shutil.which("claude") is not None

    def register(self, config: Config, **overrides: Any) -> dict[str, Any]:
        from sup_mem.registration import register_into_claude_code

        return register_into_claude_code(
            config,
            claude_dir=overrides.get("claude_dir"),
            claude_json=overrides.get("claude_json"),
            use_cli=overrides.get("use_cli", True),
        )

    def parse_transcript(self, path: Path) -> list[Turn]:
        from sup_mem.ledger import parse_transcript

        return parse_transcript(path)
