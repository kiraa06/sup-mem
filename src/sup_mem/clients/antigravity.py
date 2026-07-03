"""Antigravity (Google's agent IDE) adapter.

Antigravity is a *separate* host from Gemini CLI (they only share the ``~/.gemini/`` parent).
It has its own hooks (``~/.gemini/config/hooks.json``, a name-keyed schema) and its own MCP
registry (``~/.gemini/antigravity/mcp_config.json`` — where a working server already lives, so
that path is confirmed). Its hook stdin is camelCase (``transcriptPath``) and injection is via
the ``additionalContext`` envelope; those specifics are corroborated but not docs-confirmed
(the docs are a JS SPA), so treat the hook path as provisional until validated live.

Transcript shape verified against real logs (``~/.gemini/antigravity/brain/**/transcript.jsonl``):
each line is a step ``{type, source, content}``. We take ``USER_INPUT`` (source ``USER_EXPLICIT``)
as the user turn and the model's *prose* steps (``PLANNER_RESPONSE``/``GENERIC``, source ``MODEL``)
as the assistant turn — deliberately EXCLUDING tool-output steps (``VIEW_FILE``/``GREP_SEARCH``/
``RUN_COMMAND``/…), whose ``content`` is tool I/O, not the assistant's words (including them would
poison attribution). The ``.pb`` files under ``conversations/`` are protobuf and not used.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sup_mem.clients.base import ClientAdapter, iter_jsonl_tail
from sup_mem.registration import ANTIGRAVITY_EVENTS

if TYPE_CHECKING:
    from sup_mem.config import Config
    from sup_mem.ledger import Turn

_USER_TYPES = {"USER_INPUT"}
_ASSISTANT_PROSE_TYPES = {"PLANNER_RESPONSE", "GENERIC"}  # MODEL prose; tool-output types excluded


class AntigravityClient(ClientAdapter):
    name = "antigravity"
    events = ANTIGRAVITY_EVENTS
    dialect = "json"  # best-effort: inject via the additionalContext envelope (like Gemini)

    def is_installed(self) -> bool:
        return (Path.home() / ".gemini" / "antigravity").exists() or Path(
            "/Applications/Antigravity.app"
        ).exists()

    def register(self, config: Config, **overrides: Any) -> dict[str, Any]:
        from sup_mem.registration import register_into_antigravity

        return register_into_antigravity(
            config,
            hooks_path=overrides.get("antigravity_hooks"),
            mcp_config_path=overrides.get("antigravity_mcp"),
        )

    def parse_transcript(self, path: Path) -> list[Turn]:
        from sup_mem.ledger import Turn

        turns: list[Turn] = []
        for entry in iter_jsonl_tail(path):
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            etype = entry.get("type")
            if etype in _USER_TYPES:
                role = "user"
            elif etype in _ASSISTANT_PROSE_TYPES and entry.get("source") == "MODEL":
                role = "assistant"
            else:
                continue  # tool-output steps, SYSTEM, conversation-history, etc.
            turns.append(Turn(len(turns), role, content))
        return turns
