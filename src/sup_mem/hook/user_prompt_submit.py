"""UserPromptSubmit hook — the per-prompt hot path (HANDOVER §6.6, I2/I3).

Spawned fresh per prompt, lives milliseconds, dies. Reads the hook JSON from stdin; whatever
we print to stdout is injected into Claude's context.

Hard rules realized here:
  * I2 — nothing heavy is imported on the Tier-1 skip path. The backend (hence sqlite3 / any
    vector or embedding library) is imported LAZILY inside the Tier-2 branch only. Enforced by
    the subprocess test in §11.2.
  * I3 — Tier 0 pinned facts always; Tier 1 is a skip-gate (skip only when a trivial-turn
    pattern matches AND no never-skip cue matches); Tier 2 is real retrieval by score threshold.
  * Fail open + silent — any error injects nothing extra and exits 0 (§11.6). A broken memory
    layer must never block or delay the user's prompt.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from typing import TYPE_CHECKING

from sup_mem.config import load_config

if TYPE_CHECKING:
    from sup_mem.config import Config
    from sup_mem.models import Hit

_MEMORY_HEADER = "# Long-term memory (auto-retrieved for this turn)"
_PINNED_HEADER = "# Pinned facts"


def _read_stdin() -> dict[str, object]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_pinned(config: Config) -> str:
    try:
        return config.pinned_facts_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _should_skip(prompt: str, config: Config) -> bool:
    """Tier 1: skip only when a trivial-turn pattern matches AND no never-skip cue matches."""
    text = prompt.strip()
    if not text:
        return True
    if any(re.search(cue, text, re.IGNORECASE) for cue in config.tier1.cue_patterns):
        return False
    return any(re.search(skip, text, re.IGNORECASE) for skip in config.tier1.skip_patterns)


def _retrieve(prompt: str, config: Config) -> list[Hit]:
    # LAZY import — this line must never execute on the Tier-1 skip path (I2).
    from sup_mem.backends import get_backend

    backend = get_backend(config)
    try:
        if not backend.hook_safe:
            # e.g. qdrant + fastembed: embedding here would load a model in the short-lived
            # hook (I2). The MCP `recall` tool still works from the warm server.
            return []
        return backend.search(prompt, k=config.retrieval.k, threshold=config.retrieval.threshold)
    finally:
        backend.close()


def _format_hits(hits: list[Hit]) -> str:
    lines = [_MEMORY_HEADER]
    lines.extend(f"- {hit.text}  (relevance {hit.score:.2f})" for hit in hits)
    return "\n".join(lines)


def _log(config: Config, prompt: str, hits: list[Hit], tier: str) -> None:
    """Best-effort retrieval log for threshold tuning (§8). Never raises."""
    if not config.logging.retrieval_log:
        return
    with contextlib.suppress(Exception):
        record = {
            "query": prompt[:500],
            "tier": tier,
            "injected_ids": [h.id for h in hits],
            "scores": [round(h.score, 4) for h in hits],
        }
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with config.retrieval_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def _emit(parts: list[str]) -> None:
    if parts:
        sys.stdout.write("\n\n".join(parts) + "\n")


def main() -> int:
    try:
        data = _read_stdin()
        prompt = str(data.get("prompt", ""))
        config = load_config()
    except Exception:
        return 0  # fail open before we even know what to do

    parts: list[str] = []

    # Tier 0 — pinned facts, always, no lookup.
    pinned = _read_pinned(config)
    if pinned:
        parts.append(f"{_PINNED_HEADER}\n{pinned}")

    # Tier 1 — cheap skip-gate for trivial turns (nothing heavy imported yet).
    try:
        skip = _should_skip(prompt, config)
    except Exception:
        skip = False
    if skip:
        _emit(parts)
        _log(config, prompt, [], "skip")
        return 0

    # Tier 2 — real retrieval; lazily imports the backend and fails open.
    hits: list[Hit] = []
    try:
        hits = _retrieve(prompt, config)
    except Exception:
        hits = []
    if hits:
        parts.append(_format_hits(hits))

    _emit(parts)
    _log(config, prompt, hits, "retrieve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
