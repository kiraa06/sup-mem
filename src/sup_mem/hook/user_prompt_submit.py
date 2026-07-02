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


def _retrieve(prompt: str, config: Config) -> tuple[list[Hit], list[Hit]]:
    """Return ``(injected, pool)``: the hits to inject and the wider candidate pool.

    The pool (``ledger.pool_k`` wide, unthresholded) feeds the outcome loop: every candidate
    is logged so `sup-mem tune` can replay thresholds counterfactually (PHASE6 L4).
    """
    # LAZY import — this line must never execute on the Tier-1 skip path (I2).
    from sup_mem.backends import get_backend

    backend = get_backend(config)
    try:
        if not backend.hook_safe:
            # e.g. qdrant + fastembed: embedding here would load a model in the short-lived
            # hook (I2). The MCP `recall` tool still works from the warm server.
            return [], []
        pool_k = max(config.retrieval.k, config.ledger.pool_k)
        pool = backend.search(prompt, k=pool_k, threshold=0.0)
    finally:
        backend.close()

    try:
        from sup_mem.ranking import adjust

        pool = adjust(pool, config)  # outcome-reinforced ranking + quarantine (fail-open)
    except Exception:
        pass
    injected = [h for h in pool if h.score >= config.retrieval.threshold][: config.retrieval.k]
    return injected, pool


def _clip(text: str, limit: int) -> str:
    """Cap a memory's injected size (word-boundary cut + a recall pointer). 0 = unlimited.

    Long memories were the main context cost (~1k tokens each): inject the head as a scent
    trail; the full text stays one explicit `recall` away.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit].rsplit(None, 1)[0]
    return f"{head} … (truncated — use recall for the full memory)"


def _format_hits(hits: list[Hit], max_chars: int) -> str:
    lines = [_MEMORY_HEADER]
    lines.extend(f"- {_clip(hit.text, max_chars)}  (relevance {hit.score:.2f})" for hit in hits)
    return "\n".join(lines)


def _log(
    config: Config,
    prompt: str,
    hits: list[Hit],
    tier: str,
    session_id: str = "",
    pool: list[Hit] | None = None,
) -> None:
    """Best-effort retrieval log for threshold tuning + the outcome ledger (§8, PHASE6).

    ``candidates`` rows are ``[id, score, injected, est_tokens]`` — the Stop hook ingests
    them; `sup-mem tune` replays them. Never raises.
    """
    if not config.logging.retrieval_log:
        return
    with contextlib.suppress(Exception):
        from datetime import UTC, datetime

        injected_ids = {h.id for h in hits}
        # Token estimates reflect what injection actually costs post-clip, so roi/tune
        # arithmetic stays honest about the real context spend.
        clip = config.retrieval.max_inject_chars

        def _est(text: str) -> int:
            effective = min(len(text), clip) if clip > 0 else len(text)
            return max(1, effective // 4)

        record = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "query": prompt[:500],
            "tier": tier,
            "injected_ids": [h.id for h in hits],
            "scores": [round(h.score, 4) for h in hits],
            "candidates": [
                [h.id, round(h.score, 4), int(h.id in injected_ids), _est(h.text)]
                for h in (pool if pool is not None else hits)
            ],
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
        session_id = str(data.get("session_id", ""))
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
        _log(config, prompt, [], "skip", session_id)
        return 0

    # Tier 2 — real retrieval; lazily imports the backend and fails open.
    hits: list[Hit] = []
    pool: list[Hit] = []
    try:
        hits, pool = _retrieve(prompt, config)
    except Exception:
        hits, pool = [], []
    if hits:
        parts.append(_format_hits(hits, config.retrieval.max_inject_chars))

    _emit(parts)
    _log(config, prompt, hits, "retrieve", session_id, pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
