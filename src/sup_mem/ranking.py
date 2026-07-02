"""Outcome-reinforced ranking (docs/PHASE6-LOOP.md).

Adjusts retrieval scores with the ledger's evidence — bounded so base relevance always
dominates (L3) — and drops quarantined memories (repeatedly contradicted, never redeemed).
Advisory + fail-open (L2): any ledger problem returns the hits unchanged.

    score' = clamp01(score + boost_weight * (referenced - contradicted) / max(injected, 3))
    quarantined: contradicted >= quarantine_contradictions AND contradicted > referenced
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sup_mem.models import Hit

if TYPE_CHECKING:
    from sup_mem.config import Config


def adjust(hits: list[Hit], config: Config) -> list[Hit]:
    if not config.ledger.enabled or not hits:
        return hits
    try:
        from sup_mem.ledger import Ledger

        with Ledger(config.ledger_db_path) as ledger:
            stats = ledger.stats_for([hit.id for hit in hits])
    except Exception:
        return hits  # fail open (L2)
    if not stats:
        return hits

    led = config.ledger
    adjusted: list[Hit] = []
    for hit in hits:
        s = stats.get(hit.id)
        if s is None:
            adjusted.append(hit)
            continue
        if (
            s["contradicted"] >= led.quarantine_contradictions
            and s["contradicted"] > s["referenced"]
        ):
            continue  # quarantined — reversible by clearing its ledger rows (L3)
        boost = led.boost_weight * (s["referenced"] - s["contradicted"]) / max(s["injected"], 3)
        score = min(1.0, max(0.0, hit.score + boost))
        adjusted.append(Hit(id=hit.id, text=hit.text, score=score, metadata=hit.metadata))
    adjusted.sort(key=lambda h: h.score, reverse=True)
    return adjusted
