"""Gemini CLI adapter.

Gemini's hooks share the ``hookSpecificOutput.additionalContext`` injection envelope and the
``prompt``/``session_id``/``transcript_path`` stdin fields, but rename the events
(BeforeAgent/AfterAgent/PreCompress) and — per its docs — require the JSON envelope on stdout
rather than plain text. Hooks + MCP both live in one JSON file (``~/.gemini/settings.json``).

Transcript shape verified against real chat files (``~/.gemini/tmp/*/chats/session-*.json``):
``{messages:[{type:"user"|"gemini", content:"..."}]}``. The parser also tolerates a bare array
or a JSONL fallback and fails open. (Antigravity's ``brain/**/transcript.jsonl`` is a different,
agentic step-format — out of scope for Gemini CLI.)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sup_mem.clients.base import (
    MAX_TRANSCRIPT_BYTES,
    ClientAdapter,
    entry_role,
    entry_text,
)
from sup_mem.registration import GEMINI_EVENTS

if TYPE_CHECKING:
    from sup_mem.config import Config
    from sup_mem.ledger import Turn

_MESSAGE_KEYS = ("messages", "history", "turns", "conversation", "entries")


def _load_entries(raw: str) -> list[dict[str, Any]]:
    """Extract a list of entry dicts from Gemini's transcript, whatever its container shape."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:  # not a single JSON doc → try JSONL
        out: list[dict[str, Any]] = []
        for line in raw.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in _MESSAGE_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
    return []


class GeminiClient(ClientAdapter):
    name = "gemini"
    events = GEMINI_EVENTS
    dialect = "json"  # Gemini injects only via the additionalContext JSON envelope

    def is_installed(self) -> bool:
        return (Path.home() / ".gemini").exists() or shutil.which("gemini") is not None

    def register(self, config: Config, **overrides: Any) -> dict[str, Any]:
        from sup_mem.registration import register_into_gemini

        return register_into_gemini(config, gemini_home=overrides.get("gemini_home"))

    def parse_transcript(self, path: Path) -> list[Turn]:
        from sup_mem.ledger import Turn

        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > MAX_TRANSCRIPT_BYTES:
                    fh.seek(size - MAX_TRANSCRIPT_BYTES)
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return []
        turns: list[Turn] = []
        for entry in _load_entries(raw):
            if entry.get("isSidechain"):
                continue
            role = entry_role(entry)
            if role not in ("user", "assistant"):
                continue
            text = entry_text(entry)
            if text.strip():
                turns.append(Turn(len(turns), role, text))
        return turns
