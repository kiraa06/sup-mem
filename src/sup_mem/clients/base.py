"""Client adapters — the small seam where the three supported hosts actually differ.

Everything else (retrieval, ranking, the ledger, provenance, archival) is host-agnostic. A
client differs in exactly three ways, and this base captures all three:

  * ``register`` — where its config lives and in what format (JSON vs TOML, one file vs two).
  * ``events``   — the event *names* it fires (Claude & Codex share them; Gemini renames).
  * ``parse_transcript`` + ``dialect`` — how its session transcript is shaped on disk, and
    whether injected context goes to stdout as plain text or a JSON envelope.

The hook scripts themselves are shared: the stdin fields (``prompt``/``session_id``/
``transcript_path``) and the ``hookSpecificOutput.additionalContext`` envelope are identical
across all three, so only these edges need per-client code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config
    from sup_mem.ledger import Turn

MAX_TRANSCRIPT_BYTES = 20_000_000  # parse at most the last 20 MB of a transcript

# Tolerant role/type vocab — different hosts label turns differently; we only care about the
# two roles the attribution pass reads. Everything else (tool, system, reasoning) is skipped.
# Verified against real files: Gemini CLI labels the assistant turn "gemini".
_USER_TYPES = {"user", "user_message", "prompt", "input"}
_ASSISTANT_TYPES = {
    "assistant",
    "assistant_message",
    "response",
    "output",
    "agent",
    "model",
    "gemini",
}


def blocks_to_text(content: Any) -> str:
    """Text from a message ``content`` field: a plain string, or a list of typed blocks.

    Accepts any block carrying a ``text`` string — Claude's ``text``, Codex's
    ``input_text``/``output_text``, etc. — and drops non-text blocks (e.g. ``input_image``).
    Shared so the Codex/Gemini parsers handle block lists without importing ledger internals.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def entry_role(entry: dict[str, Any]) -> str:
    """Best-effort user/assistant role for a transcript entry, or "" to skip it."""
    role = entry.get("role")
    if role in ("user", "assistant"):
        return str(role)
    kind = str(entry.get("type", "")).lower()
    if kind in _USER_TYPES:
        return "user"
    if kind in _ASSISTANT_TYPES:
        return "assistant"
    return ""


def entry_text(entry: dict[str, Any]) -> str:
    """Best-effort turn text across the shapes hosts use (flat string, block list, nested msg)."""
    for key in ("content", "text", "message"):
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, dict):  # nested {"message": {"content": ...}}
            value = value.get("content", value.get("text"))
        text = blocks_to_text(value)
        if text.strip():
            return text
    return ""


def iter_jsonl_tail(path: Path, max_bytes: int = MAX_TRANSCRIPT_BYTES) -> Iterator[dict[str, Any]]:
    """Yield each JSON object from a (possibly huge) JSONL file, reading at most the last
    ``max_bytes``. Malformed lines are skipped; I/O errors yield nothing (fail-open)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop the partial line after the seek
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


class ClientAdapter:
    """One supported host. Subclasses set the class attrs and implement the two methods."""

    name: str = ""
    #: canonical role -> this client's event name (see registration.*_EVENTS)
    events: dict[str, str] = {}
    #: "plain" — stdout text is injected as-is (Claude, Codex); "json" — wrap in the
    #: ``{"hookSpecificOutput": {"additionalContext": ...}}`` envelope (Gemini).
    dialect: str = "plain"

    def is_installed(self) -> bool:
        """True if this host looks present on the machine (config dir or binary on PATH)."""
        raise NotImplementedError

    def register(self, config: Config, **overrides: Any) -> dict[str, Any]:
        """Wire our hooks + MCP server into this host's config. Returns a change report."""
        raise NotImplementedError

    def parse_transcript(self, path: Path) -> list[Turn]:
        """Parse this host's session transcript into ordered user/assistant turns."""
        raise NotImplementedError
