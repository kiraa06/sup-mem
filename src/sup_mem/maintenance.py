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
def backup_stores(config: Config) -> StepResult:
    targets = [p for p in (config.db_path, config.ledger_db_path) if p.exists()]
    if not targets:
        return _result("backup", "skipped", "no local databases to back up")

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

    # Retention: keep the newest N backup directories.
    backups = sorted((d for d in config.backups_dir.iterdir() if d.is_dir()), reverse=True)
    pruned = 0
    for old in backups[config.maintenance.backup_keep :]:
        shutil.rmtree(old, ignore_errors=True)
        pruned += 1
    return _result("backup", "ok", f"{', '.join(copied)} → {dest_dir.name} (pruned {pruned})")


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

    from sup_mem.ledger import Ledger, attributed_count, recommend_threshold, replay_thresholds

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
    rows = replay_thresholds(turns, config.retrieval.k, current)
    recommended = recommend_threshold(rows, current)
    if recommended == round(current, 2):
        return _result("auto-tune", "ok", f"threshold {current} already optimal")

    from sup_mem.config import load_config, render_default_toml

    updated = load_config(
        overrides={"data_dir": str(config.data_dir), "retrieval": {"threshold": recommended}}
    )
    updated.config_path.write_text(render_default_toml(updated), encoding="utf-8")
    return _result("auto-tune", "ok", f"threshold {current} → {recommended} (lossless, applied)")


# --------------------------------------------------------------------------------------------
# manifest + vacuum + health
# --------------------------------------------------------------------------------------------
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


def notify_macos(title: str, message: str) -> None:
    """Best-effort user notification; silently does nothing off-macOS or on failure."""
    with contextlib.suppress(Exception):
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{message}" with title "{title}"',
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )


# --------------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------------
_STEPS = [
    rotate_log,
    backup_stores,
    sweep_native,
    auto_tune,
    refresh_manifest,
    vacuum_stores,
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
        notify_macos("sup-mem maintain", f"{len(failures)} step(s) failed — {summary}")
    return results


def as_report(results: list[StepResult]) -> dict[str, Any]:
    return {
        "ok": all(r.status != "failed" for r in results),
        "steps": [{"name": r.name, "status": r.status, "detail": r.detail} for r in results],
    }
