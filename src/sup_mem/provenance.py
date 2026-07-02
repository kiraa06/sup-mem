"""Tamper-evident provenance chain (docs/PHASE8-TEMPORAL.md, T5).

Every write to the memory store appends an event to an HMAC hash chain living in the same
SQLite file:

    entry_hash = HMAC-SHA256(key, prev_hash ‖ canonical(event))

The key lives at ``~/.sup-mem/key`` (0600, auto-created). ``verify()`` re-walks the chain
and cross-checks every memory row's payload hash against its latest event, so a direct
``UPDATE memories SET text=...`` is caught even though rows carry no signature themselves.

Threat model, stated plainly: this is tamper-EVIDENT under a local attacker who cannot read
the key. An attacker with the key can re-chain history. Real non-repudiation needs keys that
live elsewhere; for a personal memory store this is the right-sized tool.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
EVENTS = ("stored", "superseded", "revived", "restated")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event        TEXT NOT NULL,
    memory_id    TEXT NOT NULL,
    lineage      TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    speaker      TEXT NOT NULL DEFAULT '',
    session_id   TEXT NOT NULL DEFAULT '',
    ts           TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    entry_hash   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provenance_memory ON provenance (memory_id, seq);
"""


def payload_hash(text: str, metadata: dict[str, Any]) -> str:
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode() + b"\x00" + canonical.encode()).hexdigest()


def speaker_for(source: str) -> str:
    """Best-available origin claim (T5): the chain protects its integrity, not its truth."""
    if source.startswith("mcp:"):
        return "claude"
    if source.startswith("native:"):
        return "migration"
    if source.startswith("session:"):
        return "session"
    return "unknown"


def load_or_create_key(key_path: Path) -> bytes:
    if key_path.exists():
        return bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key.hex() + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    return key


def _canonical_event(row: dict[str, Any]) -> bytes:
    fields = {
        "event": row["event"],
        "memory_id": row["memory_id"],
        "lineage": row["lineage"],
        "source": row["source"],
        "speaker": row["speaker"],
        "session_id": row["session_id"],
        "ts": row["ts"],
        "payload_hash": row["payload_hash"],
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def _entry_hash(key: bytes, prev_hash: str, row: dict[str, Any]) -> str:
    return hmac.new(key, prev_hash.encode() + _canonical_event(row), hashlib.sha256).hexdigest()


class ProvenanceChain:
    """Owns the ``provenance`` table inside an existing connection (the memory store's)."""

    def __init__(self, conn: sqlite3.Connection, key_path: Path) -> None:
        self._conn = conn
        self._key_path = key_path
        self._key: bytes | None = None
        conn.executescript(_SCHEMA)

    def _get_key(self) -> bytes:
        if self._key is None:
            self._key = load_or_create_key(self._key_path)
        return self._key

    def _tip(self) -> str:
        row = self._conn.execute(
            "SELECT entry_hash FROM provenance ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else GENESIS

    def append(
        self,
        event: str,
        *,
        memory_id: str,
        lineage: str,
        source: str,
        ts: str,
        payload: str,
        session_id: str = "",
    ) -> None:
        row = {
            "event": event,
            "memory_id": memory_id,
            "lineage": lineage,
            "source": source,
            "speaker": speaker_for(source),
            "session_id": session_id,
            "ts": ts,
            "payload_hash": payload,
        }
        prev = self._tip()
        self._conn.execute(
            "INSERT INTO provenance "
            "(event, memory_id, lineage, source, speaker, session_id, ts, payload_hash, "
            " prev_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["event"],
                row["memory_id"],
                row["lineage"],
                row["source"],
                row["speaker"],
                row["session_id"],
                row["ts"],
                row["payload_hash"],
                prev,
                _entry_hash(self._get_key(), prev, row),
            ),
        )

    def latest_payload_hashes(self) -> dict[str, str]:
        """memory_id → payload_hash of its most recent content-bearing event."""
        out: dict[str, str] = {}
        rows = self._conn.execute(
            "SELECT memory_id, event, payload_hash FROM provenance ORDER BY seq ASC"
        ).fetchall()
        for memory_id, event, digest in rows:
            if event in ("stored", "revived", "restated"):
                out[str(memory_id)] = str(digest)
        return out

    def verify(self, row_hashes: dict[str, str]) -> dict[str, Any]:
        """Walk the chain + cross-check current rows. ``row_hashes``: memory_id → live hash."""
        key = self._get_key()
        prev = GENESIS
        events = 0
        cursor = self._conn.execute("SELECT * FROM provenance ORDER BY seq ASC")
        columns = [c[0] for c in cursor.description]
        for raw in cursor.fetchall():
            record = dict(zip(columns, tuple(raw), strict=True))
            if record["prev_hash"] != prev:
                return {
                    "ok": False,
                    "events": events,
                    "reason": f"chain break at seq {record['seq']}: prev_hash mismatch",
                }
            if _entry_hash(key, prev, record) != record["entry_hash"]:
                return {
                    "ok": False,
                    "events": events,
                    "reason": f"chain break at seq {record['seq']}: entry_hash mismatch "
                    "(event edited?)",
                }
            prev = str(record["entry_hash"])
            events += 1

        expected = self.latest_payload_hashes()
        mismatched = [mid for mid, digest in row_hashes.items() if expected.get(mid) != digest]
        unrecorded = [mid for mid in row_hashes if mid not in expected]
        if mismatched:
            return {
                "ok": False,
                "events": events,
                "reason": f"{len(mismatched)} memory row(s) differ from their provenance "
                f"(edited outside sup-mem?): {', '.join(mismatched[:5])}",
            }
        if unrecorded:
            return {
                "ok": False,
                "events": events,
                "reason": f"{len(unrecorded)} memory row(s) have no provenance events: "
                f"{', '.join(unrecorded[:5])}",
            }
        return {"ok": True, "events": events, "reason": ""}
