"""Embedding provider auto-detection (HANDOVER §6.4).

Resolves a provider by priority, prints what it found, and either lets the user choose
(interactive) or auto-picks the top available one (``--yes`` / non-interactive). Probing is
fast and side-effect-free (no downloads) until a choice is made. The chosen ``(provider,
model, dim)`` is recorded into the backend's metadata to enforce I7.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sup_mem.embedding import providers
from sup_mem.embedding.base import EmbeddingError

if TYPE_CHECKING:
    from sup_mem.config import Config

Logger = Callable[[str], None]

REMEDIATION = (
    "No embedding provider is available. Pick one of:\n"
    "  • pip install 'sup-mem[qdrant]'   (bundles the fastembed CPU model — no server)\n"
    "  • run Ollama and: ollama pull nomic-embed-text\n"
    "  • start a TEI server and set TEI_URL\n"
    "  • set VOYAGE_API_KEY (hosted) or OPENAI_API_KEY (hosted)\n"
    "Then re-run: sup-mem setup --backend qdrant"
)


@dataclass(frozen=True)
class EmbeddingSelection:
    provider: str
    model: str
    dim: int | None


@dataclass
class _Candidate:
    spec: providers.ProviderSpec
    available: bool
    model: str


def _gather(config: Config) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for spec in providers.PROVIDER_SPECS:
        probe = getattr(providers, spec.probe_attr)
        try:
            available, model = probe(config)
        except Exception:
            available, model = False, ""
        candidates.append(_Candidate(spec, bool(available), str(model) or spec.default_model))
    return candidates


def _print_table(candidates: list[_Candidate], log: Logger) -> None:
    log("Detected embedding providers (→ = selected candidate):")
    log(f"    {'provider':<10} {'model':<28} {'dim':>5}  {'where':<13} {'latency':<8} available")
    for cand in candidates:
        dim = str(cand.spec.default_dim) if cand.spec.default_dim else "?"
        mark = "→" if cand.available else " "
        log(
            f"  {mark} {cand.spec.name:<10} {cand.model[:28]:<28} {dim:>5}  "
            f"{cand.spec.where:<13} {cand.spec.latency:<8} {'yes' if cand.available else 'no'}"
        )


def _prompt_choice(available: list[_Candidate], log: Logger) -> _Candidate:
    for idx, cand in enumerate(available, 1):
        log(f"  [{idx}] {cand.spec.name} ({cand.model})")
    try:
        raw = input(f"Choose an embedder [1-{len(available)}] (default 1): ").strip()
    except EOFError:
        raw = ""
    if not raw:
        return available[0]
    try:
        choice = int(raw)
    except ValueError:
        return available[0]
    if 1 <= choice <= len(available):
        return available[choice - 1]
    return available[0]


def detect_embedding_provider(
    config: Config,
    *,
    assume_yes: bool = False,
    interactive: bool | None = None,
    log: Logger | None = None,
) -> EmbeddingSelection:
    """Resolve an embedding provider (§6.4). Raises ``EmbeddingError`` when none is usable."""
    emit: Logger = log if log is not None else print

    # Priority 0 — a pinned provider is respected (validated for reachability).
    pinned = config.embedding.provider
    if pinned:
        spec = providers.SPEC_BY_NAME.get(pinned)
        if spec is None:
            raise EmbeddingError(f"Unknown pinned embedding provider {pinned!r}.")
        available, probed_model = getattr(providers, spec.probe_attr)(config)
        model = config.embedding.model or str(probed_model) or spec.default_model
        if not available:
            raise EmbeddingError(f"Pinned provider {pinned!r} is not reachable.\n{REMEDIATION}")
        emit(f"Using pinned embedding provider: {pinned} ({model}).")
        return EmbeddingSelection(pinned, model, spec.default_dim)

    candidates = _gather(config)
    _print_table(candidates, emit)
    available_cands = [c for c in candidates if c.available]
    if not available_cands:
        raise EmbeddingError(REMEDIATION)

    auto = assume_yes or interactive is False or (interactive is None and not sys.stdin.isatty())
    chosen = available_cands[0] if auto else _prompt_choice(available_cands, emit)
    emit(f"Selected embedding provider: {chosen.spec.name} ({chosen.model}).")
    return EmbeddingSelection(chosen.spec.name, chosen.model, chosen.spec.default_dim)
