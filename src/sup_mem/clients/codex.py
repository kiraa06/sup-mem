"""Codex CLI adapter.

Codex cloned Claude Code's hook contract verbatim — identical event names, identical stdin
fields (``prompt``/``session_id``/``transcript_path``), identical ``additionalContext`` stdout
envelope, plain-stdout also honored. So the only per-client work is *where* config lives
(hooks → ``~/.codex/hooks.json`` [auto-discovered]; MCP → ``~/.codex/config.toml``) and parsing
its session transcript.

Transcript shape verified against real rollout files (``~/.codex/sessions/**/rollout-*.jsonl``):
each line is ``{timestamp, type, payload}`` and the turns are ``response_item`` entries with
``payload {type:"message", role, content:[{type:input_text|output_text, text}]}``. Codex warns
the line schema may change, so the parser stays tolerant + fail-open.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sup_mem.clients.base import ClientAdapter, blocks_to_text, iter_jsonl_tail
from sup_mem.registration import CODEX_EVENTS

if TYPE_CHECKING:
    from sup_mem.config import Config
    from sup_mem.ledger import Turn


class CodexClient(ClientAdapter):
    name = "codex"
    events = CODEX_EVENTS
    dialect = "plain"  # Codex adds plain stdout as developer context, like Claude Code

    def is_installed(self) -> bool:
        return (Path.home() / ".codex").exists() or shutil.which("codex") is not None

    def register(self, config: Config, **overrides: Any) -> dict[str, Any]:
        from sup_mem.registration import register_into_codex

        return register_into_codex(config, codex_home=overrides.get("codex_home"))

    def parse_transcript(self, path: Path) -> list[Turn]:
        from sup_mem.ledger import Turn

        # Verified against real rollout files: each line is {timestamp, type, payload}. The
        # actual turns are ``response_item`` entries whose payload is
        # {type:"message", role:"user"|"assistant", content:[{type:"input_text"/"output_text",
        # text}]}. Other payload types (reasoning, function_call, developer role) are skipped.
        turns: list[Turn] = []
        for entry in iter_jsonl_tail(path):
            payload = entry.get("payload")
            msg = payload if isinstance(payload, dict) else entry
            if msg.get("type") not in ("message", None):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            text = blocks_to_text(msg.get("content"))
            if text.strip():
                turns.append(Turn(len(turns), str(role), text))
        return turns
