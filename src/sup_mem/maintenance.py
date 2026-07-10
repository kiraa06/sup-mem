"""Housekeeping for `sup-mem maintain` — install once, never think about it again.

Each step is independent, idempotent, and fail-soft: it reports ``ok | skipped | failed``
with a detail string and never aborts the run. Steps:

  rotate    — trim retrieval.jsonl to the keep window; EXACTLY rebases ledger cursors
  backup    — VACUUM INTO timestamped copies of memory.db + ledger.db, with retention
  sweep     — migrate-native stragglers (old sessions still writing built-in memory)
  auto-tune — apply the counterfactual recommendation ONLY when lossless (L4) + enough data
  manifest  — refresh the session-start manifest cache
  vacuum    — compact the live databases
  health    — backend health + I7 consistency; failures trigger a macOS notification
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config


@dataclass
class StepResult:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str


def _result(name: str, status: str, detail: str) -> StepResult:
    return StepResult(name=name, status=status, detail=detail)


# --------------------------------------------------------------------------------------------
# rotate — retrieval.jsonl keep-window + exact ledger cursor rebase
# --------------------------------------------------------------------------------------------
def rotate_log(config: Config) -> StepResult:
    path = config.retrieval_log_path
    if not path.exists():
        return _result("rotate", "skipped", "no retrieval log yet")
    cutoff = datetime.now(UTC) - timedelta(days=config.maintenance.log_keep_days)

    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    dropped_indexes: list[int] = []
    for index, raw in enumerate(lines):
        keep = False
        try:
            ts = str(json.loads(raw).get("ts", ""))
            keep = bool(ts) and datetime.fromisoformat(ts) >= cutoff
        except (ValueError, TypeError):
            keep = False  # malformed / legacy (pre-0.2.0) lines age out immediately
        if keep:
            kept.append(raw)
        else:
            dropped_indexes.append(index)

    if not dropped_indexes:
        return _result(
            "rotate", "ok", f"{len(lines)} lines, none past {config.maintenance.log_keep_days}d"
        )

    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    tmp.replace(path)

    if config.ledger_db_path.exists():
        from sup_mem.ledger import Ledger

        with Ledger(config.ledger_db_path) as ledger:
            ledger.rebase_cursors(dropped_indexes)
    return _result(
        "rotate",
        "ok",
        f"dropped {len(dropped_indexes)} old lines, kept {len(kept)}; cursors rebased",
    )


# --------------------------------------------------------------------------------------------
# backup — VACUUM INTO timestamped copies, with retention
# --------------------------------------------------------------------------------------------
def _store_fingerprint(config: Config) -> str:
    """Cheap 'did the memory store change' signal — the provenance chain length (append-only,
    grows on every store/supersede/restate). Empty string means unknown (never skip)."""
    if not config.db_path.exists():
        return ""
    conn = sqlite3.connect(str(config.db_path))
    try:
        return str(conn.execute("SELECT COUNT(*) FROM provenance").fetchone()[0])
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def backup_stores(config: Config) -> StepResult:
    targets = [p for p in (config.db_path, config.ledger_db_path) if p.exists()]
    if not targets:
        return _result("backup", "skipped", "no local databases to back up")
    config.backups_dir.mkdir(parents=True, exist_ok=True)

    # Skip when the memory store is unchanged since the last backup — otherwise daily maintain
    # piles up byte-identical copies (backup_keep × store_size dominates the footprint at scale).
    fingerprint = _store_fingerprint(config)
    marker = config.backups_dir / ".fingerprint"
    if fingerprint and marker.exists():
        with contextlib.suppress(OSError):
            if marker.read_text(encoding="utf-8").strip() == fingerprint:
                return _result("backup", "skipped", "store unchanged since last backup")

    # Microseconds keep stamps unique even for back-to-back runs; still lexically sortable.
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S.%f")
    dest_dir = config.backups_dir / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for db in targets:
        conn = sqlite3.connect(str(db))
        try:
            # VACUUM INTO writes a compacted, consistent snapshot — safe under WAL.
            conn.execute("VACUUM INTO ?", (str(dest_dir / db.name),))
        finally:
            conn.close()
        copied.append(db.name)

    # Retention: keep the newest N backup dirs (the .fingerprint marker is a file, so it's skipped).
    backups = sorted((d for d in config.backups_dir.iterdir() if d.is_dir()), reverse=True)
    pruned = 0
    for old in backups[config.maintenance.backup_keep :]:
        shutil.rmtree(old, ignore_errors=True)
        pruned += 1
    if fingerprint:
        with contextlib.suppress(OSError):
            marker.write_text(fingerprint + "\n", encoding="utf-8")
    return _result("backup", "ok", f"{', '.join(copied)} → {dest_dir.name} (pruned {pruned})")


def prune_candidates(config: Config) -> StepResult:
    """Bound the ledger ``candidates`` table — it only feeds `tune`'s counterfactual replay,
    which wants a recent window, not all history. Keeps the newest ``candidates_keep`` rows;
    ``stats`` (roi/ranking's cumulative counts) is never touched."""
    if not config.ledger_db_path.exists():
        return _result("prune-candidates", "skipped", "no ledger yet")
    keep = config.maintenance.candidates_keep
    conn = sqlite3.connect(str(config.ledger_db_path))
    try:
        total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        if total <= keep:
            return _result("prune-candidates", "ok", f"{total} rows (within keep {keep})")
        cutoff = conn.execute(
            "SELECT rowid FROM candidates ORDER BY rowid DESC LIMIT 1 OFFSET ?", (keep - 1,)
        ).fetchone()[0]
        with conn:
            conn.execute("DELETE FROM candidates WHERE rowid < ?", (cutoff,))
        return _result("prune-candidates", "ok", f"pruned {total - keep}, kept newest {keep}")
    except sqlite3.Error as exc:
        return _result("prune-candidates", "failed", str(exc))
    finally:
        conn.close()


# --------------------------------------------------------------------------------------------
# sweep — migrate-native stragglers
# --------------------------------------------------------------------------------------------
def sweep_native(config: Config) -> StepResult:
    from sup_mem.backends import get_backend
    from sup_mem.migrate import migrate_native
    from sup_mem.registration import claude_config_dir

    projects_dir = claude_config_dir() / "projects"
    if not projects_dir.is_dir():
        return _result("sweep", "skipped", "no native memory directory")
    backend = get_backend(config)
    try:
        report = migrate_native(backend, projects_dir)
    finally:
        backend.close()
    return _result(
        "sweep", "ok", f"{report['new']} new of {len(report['migrated'])} native memories"
    )


# --------------------------------------------------------------------------------------------
# auto-tune — apply only when lossless and evidenced (L4)
# --------------------------------------------------------------------------------------------
def auto_tune(config: Config) -> StepResult:
    if not config.maintenance.auto_tune:
        return _result("auto-tune", "skipped", "disabled in config")
    if not config.ledger_db_path.exists():
        return _result("auto-tune", "skipped", "no ledger yet")

    from sup_mem.ledger import (
        Ledger,
        attributed_count,
        recommend_k,
        recommend_threshold,
        replay_k,
        replay_thresholds,
    )

    with Ledger(config.ledger_db_path) as ledger:
        turns = ledger.candidate_turns()
    attributed = attributed_count(turns)
    if attributed < config.maintenance.tune_min_attributed:
        return _result(
            "auto-tune",
            "skipped",
            f"{attributed}/{config.maintenance.tune_min_attributed} attributed injections",
        )

    current = config.retrieval.threshold
    current_k = config.retrieval.k
    recommended = recommend_threshold(replay_thresholds(turns, current_k, current), current)
    k_recommended = recommend_k(replay_k(turns, current, current_k), current_k)

    changes: dict[str, object] = {}
    notes: list[str] = []
    if recommended != round(current, 2):
        changes["threshold"] = recommended
        notes.append(f"threshold {current} → {recommended}")
    if k_recommended != current_k:
        changes["k"] = k_recommended
        notes.append(f"k {current_k} → {k_recommended}")
    if not changes:
        return _result("auto-tune", "ok", f"threshold {current}, k {current_k} already optimal")

    from sup_mem.config import load_config, render_default_toml

    # One combined write — both knobs land in a single render (a second write would clobber).
    updated = load_config(overrides={"data_dir": str(config.data_dir), "retrieval": changes})
    updated.config_path.write_text(render_default_toml(updated), encoding="utf-8")
    return _result("auto-tune", "ok", f"{'; '.join(notes)} (lossless, applied)")


# --------------------------------------------------------------------------------------------
# manifest + vacuum + health
# --------------------------------------------------------------------------------------------
def run_archival_step(config: Config) -> StepResult:
    """Nightly archival (PHASE9): steady tiers always; pressure tiers when over the caps."""
    if not config.archival.enabled:
        return _result("archival", "skipped", "disabled in config")
    from sup_mem.archival import run_archival

    report = run_archival(config)
    if not report["supported"]:
        return _result("archival", "skipped", str(report["note"]))
    moved = len(report["steady"]) + len(report["pressure"])
    purged = len(report["purged"])
    detail = f"{moved} archived ({len(report['steady'])} steady), {purged} purged"
    if report["note"]:
        detail += f"; {report['note'].strip()}"
    return _result("archival", "ok", detail)


def refresh_manifest(config: Config) -> StepResult:
    from sup_mem.backends import get_backend
    from sup_mem.manifest import build_manifest

    backend = get_backend(config)
    try:
        text = build_manifest(backend, config)
    finally:
        backend.close()
    return _result("manifest", "ok", f"cache warm ({len(text)} chars)")


def vacuum_stores(config: Config) -> StepResult:
    compacted = []
    for db in (config.db_path, config.ledger_db_path):
        if not db.exists():
            continue
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
        compacted.append(db.name)
    if not compacted:
        return _result("vacuum", "skipped", "no local databases")
    return _result("vacuum", "ok", ", ".join(compacted))


def check_provenance(config: Config) -> StepResult:
    """Nightly chain verification (PHASE8 T5): tampering surfaces within a day."""
    from sup_mem.backends import get_backend

    backend = get_backend(config)
    try:
        check = getattr(backend, "verify_provenance", None)
        if not callable(check):
            return _result("provenance", "skipped", "not supported on this backend")
        report = check()
    finally:
        backend.close()
    if report["ok"]:
        detail = f"{report['events']} chain events intact"
        if report["reason"]:
            detail = report["reason"]
        return _result("provenance", "ok", detail)
    return _result("provenance", "failed", report["reason"])


def check_health(config: Config) -> StepResult:
    from sup_mem.backends import get_backend
    from sup_mem.embedding.base import EmbeddingError

    backend = get_backend(config)
    try:
        health = backend.health()
        check = getattr(backend, "check_consistency", None)
        if callable(check):
            check()  # raises EmbeddingError on I7 mismatch
    except EmbeddingError as exc:
        return _result("health", "failed", f"I7 mismatch: {exc}")
    except Exception as exc:
        return _result("health", "failed", str(exc))
    finally:
        backend.close()
    return _result("health", "ok", f"{health.get('backend')}: {health.get('count')} memories")


def notify_user(title: str, message: str) -> None:
    """Best-effort desktop notification (macOS osascript / Linux notify-send); never raises."""
    with contextlib.suppress(Exception):
        if sys.platform == "darwin":
            cmd = ["osascript", "-e", f'display notification "{message}" with title "{title}"']
        elif shutil.which("notify-send"):
            cmd = ["notify-send", "--urgency=critical", title, message]
        else:
            return  # headless box — the maintain.log still has the details
        subprocess.run(cmd, capture_output=True, timeout=10, check=False)


# --------------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------------
_STEPS = [
    rotate_log,
    backup_stores,  # pre-archival state is always in the latest snapshot
    sweep_native,
    auto_tune,
    run_archival_step,
    refresh_manifest,
    prune_candidates,
    vacuum_stores,
    check_provenance,
    check_health,
]


def run_maintenance(config: Config) -> list[StepResult]:
    results: list[StepResult] = []
    for step in _STEPS:
        try:
            results.append(step(config))
        except Exception as exc:  # fail-soft: one broken step never aborts the run
            results.append(_result(step.__name__.replace("_", "-"), "failed", str(exc)))

    failures = [r for r in results if r.status == "failed"]
    if not failures:
        with contextlib.suppress(OSError):
            config.maintain_stamp_path.write_text(
                datetime.now(UTC).isoformat() + "\n", encoding="utf-8"
            )
    elif config.maintenance.notify:
        summary = "; ".join(f"{r.name}: {r.detail[:80]}" for r in failures)
        notify_user("sup-mem maintain", f"{len(failures)} step(s) failed — {summary}")
    return results


def as_report(results: list[StepResult]) -> dict[str, Any]:
    return {
        "ok": all(r.status != "failed" for r in results),
        "steps": [{"name": r.name, "status": r.status, "detail": r.detail} for r in results],
    }
