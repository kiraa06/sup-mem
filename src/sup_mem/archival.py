"""Archival policy engine (docs/PHASE9-ARCHIVAL.md) — the three regimes of A2.

1. Steady state: structurally-outdated versions (superseded past the window) and stale
   quarantined memories move to the cold tier — evidence, never age (A1).
2. Main pressure: over ``archival.main_max_mb``, decay candidates move most-useless-first
   (ledger-ranked) until the cap is met — or candidates run out, in which case we STOP and
   say so rather than archive evidently-useful memories.
3. Archive pressure: over ``archival.archive_max_mb``, the oldest archived rows are deleted
   forever, FIFO, chain-audited.

The mechanics live on the sqlite backend; this module only decides *what* and *in which
order*. Backends without archival support are reported as such, never guessed at.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config

_MB = 1024 * 1024


def _cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _stale_quarantined_ids(config: Config) -> list[str]:
    """Quarantined per the L3 rule AND unreferenced past the quarantine window."""
    if not config.ledger_db_path.exists():
        return []
    from sup_mem.ledger import Ledger

    threshold = _cutoff(config.archival.quarantined_after_days)
    out: list[str] = []
    with Ledger(config.ledger_db_path) as ledger:
        for s in ledger.all_stats():
            quarantined = (
                s["contradicted"] >= config.ledger.quarantine_contradictions
                and s["contradicted"] > s["referenced"]
            )
            stale = not s["last_referenced"] or s["last_referenced"] < threshold
            if quarantined and stale:
                out.append(str(s["memory_id"]))
    return out


def _rank_by_uselessness(candidates: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    """Most-useless first (A1): evidenced-useless → no-evidence → (useful excluded upstream)."""
    stats: dict[str, dict[str, Any]] = {}
    if config.ledger_db_path.exists():
        from sup_mem.ledger import Ledger

        with Ledger(config.ledger_db_path) as ledger:
            stats = {s["memory_id"]: s for s in ledger.all_stats()}

    recent_ref_cutoff = _cutoff(config.archival.decay_min_age_days)
    eligible: list[dict[str, Any]] = []
    for cand in candidates:
        s = stats.get(cand["id"], {})
        last_ref = str(s.get("last_referenced", ""))
        if last_ref and last_ref >= recent_ref_cutoff:
            continue  # referenced recently → never pressure-archived (A1)
        cand = dict(cand)
        cand["_referenced"] = int(s.get("referenced", 0))
        cand["_ignored"] = int(s.get("ignored", 0))
        cand["_injected"] = int(s.get("injected", 0))
        eligible.append(cand)

    def key(c: dict[str, Any]) -> tuple[int, int, int, int, str]:
        evidenced_useless = c["_injected"] > 0 and c["_referenced"] == 0
        return (
            0 if evidenced_useless else 1,  # proven-useless first, no-evidence after
            c["_referenced"],  # fewer references = more useless
            -c["_ignored"],  # more ignored-despite-chances first
            -c["_injected"],
            c["recorded_at"],  # oldest first as the final tiebreak
        )

    return sorted(eligible, key=key)


def run_archival(config: Config, *, dry_run: bool = False) -> dict[str, Any]:
    from sup_mem.backends import get_backend
    from sup_mem.backends.sqlite_fts import SqliteFtsBackend

    backend = get_backend(config)
    if not isinstance(backend, SqliteFtsBackend):
        backend.close()
        return {"supported": False, "note": "archival requires the sqlite_fts backend"}

    report: dict[str, Any] = {
        "supported": True,
        "steady": [],
        "pressure": [],
        "purged": [],
        "note": "",
    }
    try:
        report["sizes_before"] = backend.db_sizes()

        # Regime 1 — steady state: structural + proven-harmful tiers.
        steady_ids = list(
            dict.fromkeys(
                backend.superseded_before(_cutoff(config.archival.superseded_after_days))
                + _stale_quarantined_ids(config)
            )
        )
        if dry_run:
            report["steady"] = steady_ids
        elif steady_ids:
            report["steady"] = backend.archive_versions(steady_ids)

        # Regime 2 — main pressure: most-useless-first until under the cap.
        main_cap = int(config.archival.main_max_mb * _MB)
        if main_cap > 0:
            size = backend.db_sizes()["main"] if not dry_run else report["sizes_before"]["main"]
            if size > main_cap:
                ranked = _rank_by_uselessness(
                    backend.live_candidates(
                        _cutoff(config.archival.decay_min_age_days), config.archival.keep_tag
                    ),
                    config,
                )
                if dry_run:
                    report["pressure"] = [c["id"] for c in ranked]
                    report["note"] = (
                        f"main tier over cap ({size / _MB:.1f} > "
                        f"{config.archival.main_max_mb} MB); would archive up to "
                        f"{len(ranked)} candidates, most-useless first"
                    )
                else:
                    while ranked and backend.db_sizes()["main"] > main_cap:
                        batch, ranked = ranked[:25], ranked[25:]
                        report["pressure"].extend(
                            backend.archive_versions([c["id"] for c in batch])
                        )
                        backend.compact()
                    if backend.db_sizes()["main"] > main_cap:
                        report["note"] = (
                            "main tier still over cap after archiving every eligible "
                            "candidate — refusing to archive evidently-useful memories; "
                            "raise archival.main_max_mb"
                        )

        # Regime 3 — archive pressure: FIFO permanent deletion (chain-audited).
        archive_cap = int(config.archival.archive_max_mb * _MB)
        if archive_cap > 0 and not dry_run:
            report["purged"] = backend.purge_archive_fifo(archive_cap)
        elif archive_cap > 0 and dry_run:
            over = backend.db_sizes()["archive"] - archive_cap
            if over > 0:
                report["note"] += (
                    f" archive tier over cap by {over / _MB:.1f} MB; oldest archived rows "
                    "would be DELETED FOREVER (FIFO)"
                )

        if not dry_run:
            backend.compact()
        report["sizes_after"] = backend.db_sizes()
        return report
    finally:
        backend.close()
