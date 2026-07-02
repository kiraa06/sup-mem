"""Default backend: SQLite FTS5 + BM25 (HANDOVER §6.2, I8) — bitemporal since v0.5 (PHASE8).

Zero optional dependencies — a single embedded file at ``~/.sup-mem/memory.db``, no
server, no model, no Docker. Uses the stdlib ``sqlite3`` module only.

Scoring (see §14 for the documented open decision):
  * FTS5's ``bm25()`` C function fetches + pre-orders candidates against the porter-stemmed
    inverted index — fast enough for the <10 ms/10k budget (§8).
  * The reported 0..1 score is **term coverage** (fraction of the query's distinct terms the
    memory contains) lifted toward 1 by a **tunable logistic squash of BM25**:
    ``score = coverage + (1 - coverage) * squash(bm25)``. Coverage is the robust floor — it
    stays well-behaved in small stores where BM25's IDF turns negative (a term present in most
    documents) — while the squash (``fts.squash_midpoint`` / ``fts.squash_steepness``) refines
    ranking where BM25 is meaningful. This keeps ``threshold`` portable across backends (§6.1).
  * ``fts.k1`` / ``fts.b`` are kept in config for completeness, but SQLite FTS5 fixes k1/b
    internally (1.2 / 0.75) and does not expose them to ``bm25()``.

Temporal model (docs/PHASE8-TEMPORAL.md):
  * Append-only versions (T1): rows are superseded (``superseded_at`` set), never destroyed.
  * ``recorded_at``/``superseded_at`` = transaction time (exact); ``valid_from`` = advisory
    event time (T2). ``lineage`` ties versions of the same fact line together.
  * Supersession only for *specific* sources (T3); generic buckets coexist. Re-storing a
    superseded version's exact text revives it.
  * The hot path (search without ``as_of``, manifest, health.count) sees live rows only (T4).
  * Every write appends to the tamper-evident provenance chain (T5) when enabled.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sup_mem.backends.base import MemoryBackend, ProgressCallback
from sup_mem.models import Hit, Metadata
from sup_mem.provenance import ProvenanceChain, payload_hash

if TYPE_CHECKING:
    from sup_mem.config import Config

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TERMS = 32  # cap MATCH size; long prompts don't need every token
_POOL_CAP = 256  # candidates fetched (bm25-ordered) before coverage re-ranking
_SCHEMA_VERSION = 2

# Sources that are buckets, not identities: a new text under them is a NEW fact line, never a
# supersession of the previous one (T3).
GENERIC_SOURCES = frozenset({"", "mcp:remember"})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _make_id(text: str, source: str) -> str:
    """Deterministic id from (source, text) → idempotent re-stores (§6.1)."""
    digest = hashlib.sha256(f"{source}\x00{text}".encode()).hexdigest()
    return digest[:24]


def _query_terms(query: str) -> list[str]:
    """Distinct lowercased word tokens (order preserved, capped)."""
    tokens = _WORD_RE.findall(query.lower())
    return list(dict.fromkeys(str(tok) for tok in tokens))[:_MAX_QUERY_TERMS]


def _match_expression(terms: list[str]) -> str:
    """Safe FTS5 MATCH string: each term quoted as a string literal and OR-ed.

    Quoting means user punctuation can never be parsed as an FTS5 operator, so a malformed
    query degrades to "no match" rather than raising (I2 / fail-open).
    """
    return " OR ".join(f'"{term}"' for term in terms)


class SqliteFtsBackend(MemoryBackend):
    def __init__(self, config: Config) -> None:
        self._config = config
        self._fts = config.fts
        self._lock = threading.Lock()
        config.data_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the long-lived MCP server may touch the conn from anyio
        # worker threads; all writes are serialized by self._lock.
        self._conn = sqlite3.connect(str(config.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._chain: ProvenanceChain | None = None
        self._init_schema()
        if config.provenance.enabled:
            with self._lock, self._conn:
                self._chain = ProvenanceChain(self._conn, config.provenance_key_path)
                self._genesis_events()

    # -- schema + migration -----------------------------------------------------------------
    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;

                CREATE TABLE IF NOT EXISTS memories (
                    id            TEXT PRIMARY KEY,
                    lineage       TEXT NOT NULL DEFAULT '',
                    text          TEXT NOT NULL,
                    metadata      TEXT NOT NULL DEFAULT '{}',
                    source        TEXT NOT NULL DEFAULT '',
                    valid_from    TEXT NOT NULL DEFAULT '',
                    recorded_at   TEXT NOT NULL DEFAULT '',
                    superseded_at TEXT,
                    updated_at    TEXT NOT NULL DEFAULT ''
                );
                """
            )
            columns = self._columns("memories")
            migrated_legacy = "created_at" in columns
            if migrated_legacy:  # legacy v1 layout → migrate in place (PHASE8)
                if "lineage" not in columns:
                    self._conn.execute(
                        "ALTER TABLE memories ADD COLUMN lineage TEXT NOT NULL DEFAULT ''"
                    )
                if "valid_from" not in columns:
                    self._conn.execute(
                        "ALTER TABLE memories ADD COLUMN valid_from TEXT NOT NULL DEFAULT ''"
                    )
                if "superseded_at" not in columns:
                    self._conn.execute("ALTER TABLE memories ADD COLUMN superseded_at TEXT")
                self._conn.execute("ALTER TABLE memories RENAME COLUMN created_at TO recorded_at")
            self._conn.execute("UPDATE memories SET lineage = id WHERE lineage = ''")
            self._conn.execute("UPDATE memories SET valid_from = recorded_at WHERE valid_from = ''")
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_source_live
                    ON memories (source, superseded_at);
                CREATE INDEX IF NOT EXISTS idx_memories_lineage ON memories (lineage);

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    text,
                    content='memories',
                    content_rowid='rowid',
                    tokenize='porter unicode61'
                );

                -- keep the external-content FTS index in sync with the base table
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, text)
                    VALUES ('delete', old.rowid, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, text)
                    VALUES ('delete', old.rowid, old.text);
                    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
                END;
                """
            )
            if migrated_legacy:
                # Re-derive the FTS index from the migrated base table (spec: Migration).
                self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _genesis_events(self) -> None:
        """Pre-provenance rows get a genesis `stored` event so `verify` covers them (PHASE8)."""
        assert self._chain is not None
        recorded = self._chain.latest_payload_hashes()
        rows = self._conn.execute(
            "SELECT id, lineage, text, metadata, source, recorded_at FROM memories"
        ).fetchall()
        for row in rows:
            if str(row["id"]) in recorded:
                continue
            self._chain.append(
                "stored",
                memory_id=str(row["id"]),
                lineage=str(row["lineage"]),
                source=str(row["source"]),
                ts=str(row["recorded_at"]),
                payload=payload_hash(str(row["text"]), _load_meta(row["metadata"])),
            )

    # -- writes ---------------------------------------------------------------------------
    def _append_event(self, event: str, **kwargs: Any) -> None:
        if self._chain is not None:
            self._chain.append(event, **kwargs)

    def store(self, text: str, metadata: Metadata | None = None) -> str:
        text = text.strip()
        meta = dict(metadata or {})
        source = str(meta.get("source", ""))
        session_id = str(meta.get("session_id", ""))
        mem_id = _make_id(text, source)
        now = _now_iso()
        payload = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        digest = payload_hash(text, meta)

        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT lineage, metadata, superseded_at FROM memories WHERE id = ?", (mem_id,)
            ).fetchone()
            if row is not None:
                lineage = str(row["lineage"])
                if row["superseded_at"] is not None:
                    # The exact belief is held again → revive (T3).
                    self._conn.execute(
                        "UPDATE memories SET superseded_at = NULL, metadata = ?, updated_at = ? "
                        "WHERE id = ?",
                        (payload, now, mem_id),
                    )
                    self._append_event(
                        "revived",
                        memory_id=mem_id,
                        lineage=lineage,
                        source=source,
                        ts=now,
                        payload=digest,
                        session_id=session_id,
                    )
                else:
                    self._conn.execute(
                        "UPDATE memories SET metadata = ?, source = ?, updated_at = ? WHERE id = ?",
                        (payload, source, now, mem_id),
                    )
                    if payload_hash(text, _load_meta(row["metadata"])) != digest:
                        self._append_event(
                            "restated",
                            memory_id=mem_id,
                            lineage=lineage,
                            source=source,
                            ts=now,
                            payload=digest,
                            session_id=session_id,
                        )
                return mem_id

            # New version. A specific source supersedes its own live predecessors (T3).
            lineage = mem_id
            if source not in GENERIC_SOURCES:
                predecessors = self._conn.execute(
                    "SELECT id, lineage FROM memories WHERE source = ? AND superseded_at IS NULL",
                    (source,),
                ).fetchall()
                for pred in predecessors:
                    self._conn.execute(
                        "UPDATE memories SET superseded_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, str(pred["id"])),
                    )
                    self._append_event(
                        "superseded",
                        memory_id=str(pred["id"]),
                        lineage=str(pred["lineage"]),
                        source=source,
                        ts=now,
                        payload=digest,  # hash of the successor that displaced it
                        session_id=session_id,
                    )
                if predecessors:
                    lineage = str(predecessors[0]["lineage"])

            valid_from = str(meta.get("valid_from") or now)
            self._conn.execute(
                "INSERT INTO memories "
                "(id, lineage, text, metadata, source, valid_from, recorded_at, superseded_at, "
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (mem_id, lineage, text, payload, source, valid_from, now, now),
            )
            self._append_event(
                "stored",
                memory_id=mem_id,
                lineage=lineage,
                source=source,
                ts=now,
                payload=digest,
                session_id=session_id,
            )
        return mem_id

    # -- reads ----------------------------------------------------------------------------
    def _squash(self, rank: float) -> float:
        """Logistic squash of the (negative) BM25 rank → 0..1, monotonic in relevance."""
        relevance = -rank  # bm25() is <= 0 for matches; larger magnitude = better match
        x = -self._fts.squash_steepness * (relevance - self._fts.squash_midpoint)
        try:
            return 1.0 / (1.0 + math.exp(x))
        except OverflowError:
            return 0.0 if x > 0 else 1.0

    def search(self, query: str, k: int, threshold: float, as_of: str | None = None) -> list[Hit]:
        if k <= 0:
            return []
        terms = _query_terms(query)
        if not terms:
            return []
        if as_of is None:
            temporal = "m.superseded_at IS NULL"
        else:
            # Transaction-time as-of (T2): versions live in the store at that instant.
            temporal = (
                "m.recorded_at <= :asof AND (m.superseded_at IS NULL OR m.superseded_at > :asof)"
            )
        sql = f"""
            SELECT m.id AS id, m.text AS text, m.metadata AS metadata,
                   m.lineage AS lineage, m.recorded_at AS recorded_at,
                   m.superseded_at AS superseded_at, bm25(memories_fts) AS rank
            FROM memories_fts
            JOIN memories AS m ON m.rowid = memories_fts.rowid
            WHERE memories_fts MATCH :match AND {temporal}
            ORDER BY rank
            LIMIT :cap
            """
        bind: dict[str, Any] = {"match": _match_expression(terms), "cap": _POOL_CAP}
        if as_of is not None:
            bind["asof"] = as_of
        rows = self._conn.execute(sql, bind).fetchall()

        n_terms = len(terms)
        term_set = set(terms)
        # (score, -bm25_order) — bm25 order breaks coverage ties (earlier = stronger).
        scored: list[tuple[float, int, Any]] = []
        for order, row in enumerate(rows):
            doc_terms = set(_WORD_RE.findall(str(row["text"]).lower()))
            coverage = sum(1 for term in term_set if term in doc_terms) / n_terms
            score = coverage + (1.0 - coverage) * self._squash(float(row["rank"]))
            scored.append((score, -order, row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        hits: list[Hit] = []
        for score, _, row in scored:
            if score < threshold:
                break  # sorted best-first → nothing remaining can clear the threshold
            metadata = _load_meta(row["metadata"])
            # Reserved temporal keys for the recall CLI / diff view (underscore-prefixed).
            metadata["_lineage"] = str(row["lineage"])
            metadata["_recorded_at"] = str(row["recorded_at"])
            metadata["_superseded_at"] = row["superseded_at"]
            hits.append(
                Hit(id=str(row["id"]), text=str(row["text"]), score=score, metadata=metadata)
            )
            if len(hits) >= k:
                break
        return hits

    def fetch(self, memory_ids: list[str]) -> dict[str, str]:
        # Deliberately unfiltered: the ledger attributes injections of now-superseded
        # versions, so old texts must stay resolvable (T4).
        if not memory_ids:
            return {}
        marks = ",".join("?" for _ in memory_ids)
        rows = self._conn.execute(
            f"SELECT id, text FROM memories WHERE id IN ({marks})", memory_ids
        ).fetchall()
        return {str(row["id"]): str(row["text"]) for row in rows}

    def current_versions(self, lineages: list[str]) -> dict[str, dict[str, str]]:
        """Live version per lineage (for the recall CLI's --diff-now)."""
        if not lineages:
            return {}
        marks = ",".join("?" for _ in lineages)
        rows = self._conn.execute(
            f"SELECT lineage, id, text, recorded_at FROM memories "
            f"WHERE lineage IN ({marks}) AND superseded_at IS NULL",
            lineages,
        ).fetchall()
        return {
            str(row["lineage"]): {
                "id": str(row["id"]),
                "text": str(row["text"]),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in rows
        }

    # -- introspection --------------------------------------------------------------------
    def manifest(self, max_topics: int) -> list[str]:
        """Distinct topics/tags by frequency over LIVE versions, capped at ``max_topics``."""
        if max_topics <= 0:
            return []
        counter: Counter[str] = Counter()
        for (raw,) in self._conn.execute(
            "SELECT metadata FROM memories WHERE superseded_at IS NULL"
        ):
            meta = _load_meta(raw)
            topic = meta.get("topic")
            if isinstance(topic, str) and topic.strip():
                counter[topic.strip()] += 1
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            if isinstance(tags, list):
                for tag in tags:
                    if str(tag).strip():
                        counter[str(tag).strip()] += 1
        return [topic for topic, _ in counter.most_common(max_topics)]

    def health(self) -> dict[str, Any]:
        live = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE superseded_at IS NULL"
            ).fetchone()[0]
        )
        total = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        latest = str(
            self._conn.execute("SELECT COALESCE(MAX(updated_at), '') FROM memories").fetchone()[0]
        )
        return {
            "backend": "sqlite_fts",
            "count": live,  # the hot path's notion of "how many memories" (T4)
            "versions": total,
            "embedding": None,  # lexical backend — no embedding model (I7 not applicable)
            "revision": f"{live}:{latest}",  # manifest cache key (§6.7)
            "db_path": str(self._config.db_path),
        }

    def verify_provenance(self) -> dict[str, Any]:
        """Chain + row-hash verification (T5). See `sup-mem verify`."""
        if self._chain is None:
            return {"ok": True, "events": 0, "reason": "provenance disabled in config"}
        row_hashes: dict[str, str] = {}
        for row in self._conn.execute("SELECT id, text, metadata FROM memories"):
            row_hashes[str(row["id"])] = payload_hash(str(row["text"]), _load_meta(row["metadata"]))
        with self._lock:
            return self._chain.verify(row_hashes)

    def reindex(self, progress: ProgressCallback | None = None) -> None:
        """No-op semantically for a lexical store; we rebuild the FTS index defensively (§6.2)."""
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
        if progress is not None:
            total = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            progress(total, total)

    def close(self) -> None:
        self._conn.close()


def _load_meta(raw: Any) -> Metadata:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
