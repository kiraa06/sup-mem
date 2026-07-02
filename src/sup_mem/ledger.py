"""The outcome ledger — sup-mem's closed loop (docs/PHASE6-LOOP.md).

Records every retrieval candidate the hook logs, then (from the Stop hook) attributes each
*injected* memory against the session transcript:

  referenced   — the assistant's response used the memory's distinctive tokens
  ignored      — injected but never referenced
  contradicted — referenced, then corrected by the user in the very next turn

Everything here is advisory and fail-open (L2): stdlib sqlite3 in its own file
(``~/.sup-mem/ledger.db``), independent of the memory backend (L5). Attribution runs only in
the Stop hook, off the hot path (L1). Nothing is ever hard-deleted (L3).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config

_MAX_TRANSCRIPT_BYTES = 20_000_000  # cap: parse at most the last 20 MB of a huge transcript
_TOKEN_CAP = 48  # distinctive tokens kept per memory
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_STOPWORD_TEXT = (
    "the a an and or of to in for on with is are was were be been being this that these "
    "those it its as at by from into over under after before we you i they he she our your "
    "their his her have has had do does did will would should could can may might must not "
    "no yes if then than when where which who whom what why how all any both each more most "
    "other some such only own same so too very just about"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def distinctive_tokens(text: str, cap: int = _TOKEN_CAP) -> set[str]:
    """Tokens that identify this memory: long words, identifiers, anything with a digit."""
    ordered = list(dict.fromkeys(_WORD_RE.findall(text.lower())))
    picked = [
        tok
        for tok in ordered
        if tok not in _STOPWORDS and (len(tok) >= 5 or any(ch.isdigit() for ch in tok))
    ]
    return set(picked[:cap])


# --------------------------------------------------------------------------------------------
# Transcript parsing (shape verified against real Claude Code session files)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    index: int
    role: str  # "user" | "assistant"
    text: str


def _block_text(content: Any) -> str:
    """Text from a message content field: plain string, or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return ""


def parse_transcript(path: Path) -> list[Turn]:
    """Ordered main-chain user/assistant turns. Sidechains (subagents) are skipped; user
    entries whose content is a block list (tool results) are not real prompts and are skipped."""
    turns: list[Turn] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _MAX_TRANSCRIPT_BYTES:
                fh.seek(size - _MAX_TRANSCRIPT_BYTES)
                fh.readline()  # drop the partial line after seeking
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain"):
                    continue
                etype = entry.get("type")
                if etype not in ("user", "assistant"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if etype == "user" and not isinstance(content, str):
                    continue  # tool results ride in user-role entries with block lists
                text = _block_text(content)
                if text.strip():
                    turns.append(Turn(len(turns), etype, text))
    except OSError:
        return []
    return turns


# --------------------------------------------------------------------------------------------
# Counterfactual threshold replay (shared by `sup-mem tune` and maintain's auto-tune, L4)
# --------------------------------------------------------------------------------------------

CandidateTurn = list[dict[str, Any]]


def attributed_count(turns: list[CandidateTurn]) -> int:
    return sum(1 for turn in turns for c in turn if c["injected"] and c["outcome"])


def replay_thresholds(turns: list[CandidateTurn], k: int, current: float) -> list[dict[str, float]]:
    """Replay every logged turn at a grid of thresholds against recorded outcomes.

    Honest by construction (L4): only injections that actually happened have outcomes;
    candidates that were below the live threshold count as ``unknown`` when a lower
    threshold would have injected them.
    """
    grid = sorted({round(0.05 * i, 2) for i in range(1, 20)} | {round(current, 2)})
    rows: list[dict[str, float]] = []
    for theta in grid:
        kept_ref = lost_ref = kept_ign = cut_ign = unknown_added = 0
        tokens_total = 0
        for turn in turns:
            would = [c for c in turn if c["score"] >= theta][:k]
            would_ids = {c["memory_id"] for c in would}
            tokens_total += sum(c["tokens"] for c in would)
            for cand in turn:
                inj, outcome = bool(cand["injected"]), str(cand["outcome"])
                in_would = cand["memory_id"] in would_ids
                if inj and outcome in ("referenced", "contradicted"):
                    kept_ref += in_would and outcome == "referenced"
                    lost_ref += (not in_would) and outcome == "referenced"
                elif inj and outcome == "ignored":
                    kept_ign += in_would
                    cut_ign += not in_would
                elif not inj and in_would:
                    unknown_added += 1  # below the live threshold then → outcome unknown (L4)
        rows.append(
            {
                "theta": theta,
                "kept_ref": kept_ref,
                "lost_ref": lost_ref,
                "kept_ign": kept_ign,
                "cut_ign": cut_ign,
                "unknown": unknown_added,
                "tok_per_turn": tokens_total / max(len(turns), 1),
            }
        )
    return rows


def recommend_threshold(rows: list[dict[str, float]], current: float) -> float:
    """The highest threshold that loses zero referenced injections; else the current one."""
    keepers = [r for r in rows if r["lost_ref"] == 0]
    return float(max(keepers, key=lambda r: r["theta"])["theta"]) if keepers else current


def _find_prompt_turn(turns: list[Turn], query: str) -> int:
    """Index of the user turn carrying this logged query (prefix match); -1 if not found."""
    needle = " ".join(query.split())[:200]
    if not needle:
        return -1
    for turn in turns:
        if turn.role == "user" and " ".join(turn.text.split())[:200].startswith(needle):
            return turn.index
    return -1


# --------------------------------------------------------------------------------------------
# Ledger store
# --------------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    session_id  TEXT NOT NULL,
    line_no     INTEGER NOT NULL,           -- retrieval.jsonl line index (global, 0-based)
    memory_id   TEXT NOT NULL,
    query       TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL DEFAULT 0,
    injected    INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    ts          TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL DEFAULT '',   -- '', referenced, ignored, contradicted
    prompt_turn INTEGER NOT NULL DEFAULT -1,
    contra_done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, line_no, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_outcome ON candidates (session_id, outcome);

CREATE TABLE IF NOT EXISTS cursors (
    session_id TEXT PRIMARY KEY,
    log_line   INTEGER NOT NULL DEFAULT 0   -- next retrieval.jsonl line index to process
);

CREATE TABLE IF NOT EXISTS stats (
    memory_id       TEXT PRIMARY KEY,
    injected        INTEGER NOT NULL DEFAULT 0,
    referenced      INTEGER NOT NULL DEFAULT 0,
    ignored         INTEGER NOT NULL DEFAULT 0,
    contradicted    INTEGER NOT NULL DEFAULT 0,
    tokens          INTEGER NOT NULL DEFAULT 0,
    last_injected   TEXT NOT NULL DEFAULT '',
    last_referenced TEXT NOT NULL DEFAULT ''
);
"""


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    # -- reads used on the hot path (one indexed lookup, L1) -------------------------------
    def stats_for(self, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not memory_ids:
            return {}
        marks = ",".join("?" for _ in memory_ids)
        rows = self._conn.execute(
            f"SELECT * FROM stats WHERE memory_id IN ({marks})", memory_ids
        ).fetchall()
        return {row["memory_id"]: dict(row) for row in rows}

    def all_stats(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM stats ORDER BY tokens DESC").fetchall()
        return [dict(row) for row in rows]

    def candidate_turns(self) -> list[list[dict[str, Any]]]:
        """All logged turns as candidate lists (for `tune`'s counterfactual replay)."""
        rows = self._conn.execute(
            "SELECT * FROM candidates ORDER BY session_id, line_no, score DESC"
        ).fetchall()
        turns: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for row in rows:
            turns.setdefault((row["session_id"], row["line_no"]), []).append(dict(row))
        return list(turns.values())

    def rebase_cursors(self, dropped_lines: list[int]) -> None:
        """After retrieval-log rotation, shift each session cursor down by the number of
        dropped line indexes below it, so cursors keep matching the rewritten file.

        (Old ``candidates.line_no`` values keep their pre-rotation numbering — they are only
        an idempotency key, never re-read from the file; a collision would need one session
        to span a rotation by 80+ turns, and costs at most one skipped advisory row.)
        """
        if not dropped_lines:
            return
        import bisect

        dropped = sorted(dropped_lines)
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT session_id, log_line FROM cursors").fetchall()
            for row in rows:
                shift = bisect.bisect_left(dropped, int(row["log_line"]))
                self._conn.execute(
                    "UPDATE cursors SET log_line = ? WHERE session_id = ?",
                    (max(int(row["log_line"]) - shift, 0), row["session_id"]),
                )

    def pending_injected_ids(self, session_id: str) -> list[str]:
        """Memory ids injected this session that still await attribution."""
        rows = self._conn.execute(
            "SELECT DISTINCT memory_id FROM candidates "
            "WHERE session_id = ? AND injected = 1 AND outcome = ''",
            (session_id,),
        ).fetchall()
        return [str(row["memory_id"]) for row in rows]

    # -- attribution (Stop hook only, L1) ---------------------------------------------------
    def _log_cursor(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT log_line FROM cursors WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["log_line"]) if row else 0

    def ingest_log(self, session_id: str, log_path: Path) -> int:
        """Pull this session's new retrieval-log lines into ``candidates``. Returns new rows."""
        cursor = self._log_cursor(session_id)
        added = 0
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        with self._lock, self._conn:
            for line_no, raw in enumerate(lines):
                if line_no < cursor:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if record.get("session_id") != session_id:
                    continue
                for cand in record.get("candidates", []):
                    try:
                        mem_id, score, injected, tokens = (
                            str(cand[0]),
                            float(cand[1]),
                            int(cand[2]),
                            int(cand[3]),
                        )
                    except (TypeError, ValueError, IndexError):
                        continue
                    self._conn.execute(
                        "INSERT OR IGNORE INTO candidates "
                        "(session_id, line_no, memory_id, query, score, injected, tokens, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            line_no,
                            mem_id,
                            str(record.get("query", "")),
                            score,
                            injected,
                            tokens,
                            str(record.get("ts", "")),
                        ),
                    )
                    added += 1
            self._conn.execute(
                "INSERT INTO cursors (session_id, log_line) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET log_line = excluded.log_line",
                (session_id, len(lines)),
            )
        return added

    def _bump(self, memory_id: str, column: str, tokens: int = 0, ts: str = "") -> None:
        self._conn.execute(
            "INSERT INTO stats (memory_id) VALUES (?) ON CONFLICT(memory_id) DO NOTHING",
            (memory_id,),
        )
        self._conn.execute(
            f"UPDATE stats SET {column} = {column} + 1, tokens = tokens + ? WHERE memory_id = ?",
            (tokens, memory_id),
        )
        if column == "injected":
            self._conn.execute(
                "UPDATE stats SET last_injected = ? WHERE memory_id = ?", (ts, memory_id)
            )
        elif column == "referenced":
            self._conn.execute(
                "UPDATE stats SET last_referenced = ? WHERE memory_id = ?", (ts, memory_id)
            )

    def attribute(
        self, session_id: str, turns: list[Turn], texts: dict[str, str], config: Config
    ) -> dict[str, int]:
        """Attribute this session's pending injected candidates against the transcript."""
        report = {"referenced": 0, "ignored": 0, "contradicted": 0}
        now = _now_iso()
        led = config.ledger

        with self._lock, self._conn:
            # 1) fresh injections → referenced | ignored
            pending = self._conn.execute(
                "SELECT rowid, * FROM candidates "
                "WHERE session_id = ? AND injected = 1 AND outcome = ''",
                (session_id,),
            ).fetchall()
            for row in pending:
                text = texts.get(row["memory_id"], "")
                tokens = distinctive_tokens(text) if text else set()
                prompt_turn = _find_prompt_turn(turns, row["query"])
                after = [t for t in turns if t.role == "assistant" and t.index > prompt_turn]
                assistant_blob = " ".join(t.text for t in after).lower()
                assistant_tokens = set(_WORD_RE.findall(assistant_blob))
                needed = max(led.min_overlap_tokens, math.ceil(led.overlap_fraction * len(tokens)))
                outcome = (
                    "referenced"
                    if tokens and len(tokens & assistant_tokens) >= needed
                    else "ignored"
                )
                self._conn.execute(
                    "UPDATE candidates SET outcome = ?, prompt_turn = ? WHERE rowid = ?",
                    (outcome, prompt_turn, row["rowid"]),
                )
                self._bump(row["memory_id"], "injected", tokens=row["tokens"], ts=now)
                self._bump(row["memory_id"], outcome, ts=now)
                report[outcome] += 1

            # 2) retroactive contradiction check: referenced + a later user turn exists
            patterns = [re.compile(p, re.IGNORECASE) for p in led.correction_patterns]
            referenced = self._conn.execute(
                "SELECT rowid, * FROM candidates "
                "WHERE session_id = ? AND outcome = 'referenced' AND contra_done = 0",
                (session_id,),
            ).fetchall()
            for row in referenced:
                next_user = next(
                    (
                        t
                        for t in turns
                        if t.role == "user" and t.index > max(int(row["prompt_turn"]), -1) + 1
                    ),
                    None,
                )
                if next_user is None:
                    continue  # no later user turn yet — re-check on a future Stop
                if any(p.search(next_user.text) for p in patterns):
                    self._conn.execute(
                        "UPDATE candidates SET outcome = 'contradicted', contra_done = 1 "
                        "WHERE rowid = ?",
                        (row["rowid"],),
                    )
                    self._conn.execute(
                        "UPDATE stats SET referenced = MAX(referenced - 1, 0), "
                        "contradicted = contradicted + 1 WHERE memory_id = ?",
                        (row["memory_id"],),
                    )
                    report["contradicted"] += 1
                else:
                    self._conn.execute(
                        "UPDATE candidates SET contra_done = 1 WHERE rowid = ?", (row["rowid"],)
                    )
        return report

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
