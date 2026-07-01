"""Default backend: SQLite FTS5 + BM25 (HANDOVER §6.2, I8).

Zero optional dependencies — a single embedded file at ``~/.claude-memory/memory.db``, no
server, no model, no Docker. Uses the stdlib ``sqlite3`` module only.

Scoring (see §14 for the documented open decision):
  * FTS5's built-in ``bm25()`` C function ranks candidates against the porter-stemmed
    inverted index — fast enough for the <10 ms/10k budget (§8).
  * That raw rank (negative; more-negative = better) is squashed to 0..1 with a **tunable
    logistic** so ``threshold`` is portable across backends (§6.1). The live knobs are
    ``fts.squash_midpoint`` / ``fts.squash_steepness``.
  * ``fts.k1`` / ``fts.b`` are kept in config for completeness, but SQLite FTS5 fixes k1/b
    internally (1.2 / 0.75) and does not expose them to ``bm25()``; the squash is therefore
    the effective FTS tuning surface for v1.
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

from claude_memory.backends.base import MemoryBackend, ProgressCallback
from claude_memory.models import Hit, Metadata

if TYPE_CHECKING:
    from claude_memory.config import Config

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MAX_QUERY_TERMS = 32  # cap MATCH size; long prompts don't need every token


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _make_id(text: str, source: str) -> str:
    """Deterministic id from (source, text) → idempotent re-stores (§6.1)."""
    digest = hashlib.sha256(f"{source}\x00{text}".encode()).hexdigest()
    return digest[:24]


def _match_expression(query: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH string (quoted OR-ed terms).

    Quoting each token as a string literal means user punctuation can never be parsed as an
    FTS5 operator, so a malformed query degrades to "no match" rather than raising (I2/fail-open).
    """
    tokens = _WORD_RE.findall(query.lower())[:_MAX_QUERY_TERMS]
    if not tokens:
        return None
    return " OR ".join(f'"{tok}"' for tok in tokens)


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
        self._init_schema()

    # -- schema ---------------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;

                CREATE TABLE IF NOT EXISTS memories (
                    id         TEXT PRIMARY KEY,
                    text       TEXT NOT NULL,
                    metadata   TEXT NOT NULL DEFAULT '{}',
                    source     TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

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

    # -- writes ---------------------------------------------------------------------------
    def store(self, text: str, metadata: Metadata | None = None) -> str:
        text = text.strip()
        meta = dict(metadata or {})
        source = str(meta.get("source", ""))
        mem_id = _make_id(text, source)
        now = _now_iso()
        payload = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        with self._lock, self._conn:
            exists = self._conn.execute("SELECT 1 FROM memories WHERE id = ?", (mem_id,)).fetchone()
            if exists:
                # Same (text, source): refresh metadata + updated_at, preserve created_at.
                self._conn.execute(
                    "UPDATE memories SET metadata = ?, source = ?, updated_at = ? WHERE id = ?",
                    (payload, source, now, mem_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO memories (id, text, metadata, source, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (mem_id, text, payload, source, now, now),
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

    def search(self, query: str, k: int, threshold: float) -> list[Hit]:
        if k <= 0:
            return []
        match = _match_expression(query)
        if match is None:
            return []
        rows = self._conn.execute(
            """
            SELECT m.id AS id, m.text AS text, m.metadata AS metadata,
                   bm25(memories_fts) AS rank
            FROM memories_fts
            JOIN memories AS m ON m.rowid = memories_fts.rowid
            WHERE memories_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, k),
        ).fetchall()

        hits: list[Hit] = []
        for row in rows:
            score = self._squash(float(row["rank"]))
            if score < threshold:
                continue  # rank order == relevance order, so remaining rows only score lower
            hits.append(
                Hit(
                    id=str(row["id"]),
                    text=str(row["text"]),
                    score=score,
                    metadata=_load_meta(row["metadata"]),
                )
            )
        return hits

    # -- introspection --------------------------------------------------------------------
    def manifest(self, max_topics: int) -> list[str]:
        """Distinct topics/tags by frequency, capped at ``max_topics``.

        The scale-aware summarization layer (full-list vs clustered, token budget, caching)
        lives in ``manifest.py`` (§6.7) and builds on top of this raw, capped list.
        """
        if max_topics <= 0:
            return []
        counter: Counter[str] = Counter()
        for (raw,) in self._conn.execute("SELECT metadata FROM memories"):
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
        count = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        return {
            "backend": "sqlite_fts",
            "count": count,
            "embedding": None,  # lexical backend — no embedding model (I7 not applicable)
            "db_path": str(self._config.db_path),
        }

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
