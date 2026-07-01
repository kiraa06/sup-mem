"""Scale-aware topic manifest injected at session start (HANDOVER §6.7, I10).

Small store  (< ``manifest.full_below``): list distinct topics verbatim.
Large store  (>= ``manifest.full_below``): the same frequency-ranked source list, summarized
             within a token budget with a ``(+N more)`` tail so we never dump tens of
             thousands of tags into context.

The result is cached to disk keyed on the backend's store *revision*, so an unchanged store
does no topic scan on subsequent session starts (§8, §11.7). This module is backend-agnostic:
it uses only the ``MemoryBackend`` interface.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_memory.backends.base import MemoryBackend
    from claude_memory.config import Config


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _revision(health: dict[str, Any]) -> str:
    rev = health.get("revision")
    return str(rev) if rev is not None else str(health.get("count", 0))


def _read_cache(config: Config) -> dict[str, Any] | None:
    try:
        with config.manifest_cache_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(config: Config, revision: str, text: str) -> None:
    with contextlib.suppress(OSError):
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with config.manifest_cache_path.open("w", encoding="utf-8") as fh:
            json.dump({"revision": revision, "text": text}, fh)


def _format(topics: list[str], count: int, config: Config) -> str:
    if not topics:
        return ""
    summarized = count >= config.manifest.full_below
    head = f"# Memory index — {count} stored" + (" (summarized)" if summarized else "")
    intro = "Topics in long-term memory (use `recall` with a focused query for details):"
    fixed = _approx_tokens(f"{head}\n{intro}\n")
    budget = config.manifest.token_budget

    shown: list[str] = []
    for topic in topics:
        trial = ", ".join([*shown, topic])
        if shown and _approx_tokens(trial) + fixed > budget:
            break
        shown.append(topic)

    body = ", ".join(shown)
    remaining = len(topics) - len(shown)
    if remaining > 0:
        body += f"  (+{remaining} more)"
    return f"{head}\n{intro}\n{body}"


def build_manifest(backend: MemoryBackend, config: Config, *, use_cache: bool = True) -> str:
    """Return the manifest text to inject at session start (``""`` when the store is empty)."""
    try:
        health = backend.health()
    except Exception:
        return ""  # fail open — a broken manifest must not break session start
    count = int(health.get("count", 0))
    if count <= 0:
        return ""

    revision = _revision(health)
    cache_on = use_cache and config.manifest.cache
    if cache_on:
        cached = _read_cache(config)
        if cached is not None and cached.get("revision") == revision:
            return str(cached.get("text", ""))  # unchanged store → no topic scan

    topics = backend.manifest(config.manifest.max_topics)
    text = _format(topics, count, config)
    if cache_on:
        _write_cache(config, revision, text)
    return text
