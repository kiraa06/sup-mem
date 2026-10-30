"""The outcome ledger — sup-mem's closed loop (docs/PHASE6-LOOP.md).

Records every retrieval candidate the hook logs, then (from the Stop hook) attributes each
*injected* memory against the session transcript:

  referenced   — the assistant's response used the memory's distinctive tokens
  ignored      — injected but never referenced
  contradicted — referenced, then corrected by the user in the very next turn
  unknown      — the prompt that triggered the injection is not in the transcript (e.g. a
                 host-delivered notification), so there is no response to judge (L4)

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config

_MAX_TRANSCRIPT_BYTES = 20_000_000  # cap: parse at most the last 20 MB of a huge transcript
_PROMPT_MATCH_CHARS = 80  # normalized query head searched for inside user turns
_PROMPT_MAX_SKEW_S = 120.0  # a text match further than this from the log ts is another turn
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
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


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _turn_key(ts: str, line_no: int) -> int:
    """Idempotency key for one logged turn: its ts in epoch microseconds.

    Stable across retrieval-log rotation, unlike the file line index — rotation renumbers
    lines, and a post-rotation line reusing an old index used to be silently dropped by
    ``INSERT OR IGNORE``. Records without a usable ts fall back to the line index.
    """
    parsed = _parse_ts(ts)
    if parsed is None:
        return line_no
    return (parsed - _EPOCH) // timedelta(microseconds=1)


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
    ts: str = ""  # host timestamp, when the transcript carries one
    # User turns only: "" = a real prompt; "meta" = a host-wrapped entry (isMeta); "summary" =
    # a compaction summary. Neither of the latter two is the user reacting to a response.
    kind: str = ""


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
    """Ordered main-chain user/assistant turns. Sidechains (subagents) and tool results (user
    entries carrying ``tool_result`` blocks) are skipped; prompts sent as text blocks (e.g.
    with an image) are kept, and host-wrapped / compaction entries are marked by ``kind``."""
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
                kind = ""
                if etype == "user":
                    if isinstance(content, list):
                        if any(
                            isinstance(block, dict) and block.get("type") == "tool_result"
                            for block in content
                        ):
                            continue  # tool results ride in user-role entries
                    elif not isinstance(content, str):
                        continue
                    if entry.get("isCompactSummary"):
                        kind = "summary"
                    elif entry.get("isMeta"):
                        kind = "meta"
                text = _block_text(content)
                if text.strip():
                    turns.append(
                        Turn(len(turns), etype, text, ts=str(entry.get("timestamp", "")), kind=kind)
                    )
    except OSError:
        return []
    return turns


# --------------------------------------------------------------------------------------------
# Counterfactual threshold replay (shared by `sup-mem tune` and maintain's auto-tune, L4)
# --------------------------------------------------------------------------------------------

CandidateTurn = list[dict[str, Any]]


_JUDGED = ("referenced", "ignored", "contradicted")


def attributed_count(turns: list[CandidateTurn]) -> int:
    return sum(1 for turn in turns for c in turn if c["injected"] and c["outcome"] in _JUDGED)


def _replay_one(turns: list[CandidateTurn], theta: float, k: int) -> dict[str, float]:
    """Count outcomes for one counterfactual policy (threshold ``theta``, top-``k``).

    Honest by construction (L4): only injections that actually happened have outcomes;
    candidates that were never injected live count as ``unknown`` when this policy would
    have injected them (turn lists are score-DESC, so ``[:k]`` is a true top-k).
    """
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
                unknown_added += 1  # never injected live → outcome unknown (L4)
    return {
        "kept_ref": kept_ref,
        "lost_ref": lost_ref,
        "kept_ign": kept_ign,
        "cut_ign": cut_ign,
        "unknown": unknown_added,
        "tok_per_turn": tokens_total / max(len(turns), 1),
    }


def replay_thresholds(turns: list[CandidateTurn], k: int, current: float) -> list[dict[str, float]]:
    """Replay every logged turn at a grid of thresholds against recorded outcomes."""
    grid = sorted({round(0.05 * i, 2) for i in range(1, 20)} | {round(current, 2)})
    return [{"theta": theta, **_replay_one(turns, theta, k)} for theta in grid]


def recommend_threshold(rows: list[dict[str, float]], current: float) -> float:
    """The highest threshold that loses zero referenced injections; else the current one."""
    keepers = [r for r in rows if r["lost_ref"] == 0]
    return float(max(keepers, key=lambda r: r["theta"])["theta"]) if keepers else current


def replay_k(
    turns: list[CandidateTurn], threshold: float, current_k: int
) -> list[dict[str, float]]:
    """Replay every logged turn at k = 1..current_k+1 under the live threshold.

    The mirror of :func:`replay_thresholds` on the other retrieval knob: how many of the
    actually-referenced injections would a smaller k have kept, and at what token cost. The
    ``current_k + 1`` row is included for honesty — it can only add ``unknown`` outcomes,
    which is exactly why raising k is a guess while lowering it is evidence-based.
    """
    grid = sorted(set(range(1, current_k + 2)))
    return [{"k": float(kk), **_replay_one(turns, threshold, kk)} for kk in grid]


def recommend_k(rows: list[dict[str, float]], current_k: int) -> int:
    """The lowest k (≤ current) that loses zero referenced injections; else the current one.

    Only shrinking is recommended: a larger k would inject candidates whose outcomes were
    never observed (L4) — the same asymmetry as the threshold recommendation.
    """
    keepers = [r for r in rows if r["lost_ref"] == 0 and r["k"] <= current_k]
    return int(min(keepers, key=lambda r: r["k"])["k"]) if keepers else current_k


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _find_prompt_turn(turns: list[Turn], query: str, ts: str = "") -> int:
    """Index of the user turn that carried this logged query; -1 if it cannot be located.

    A substring match, not a prefix one: hosts wrap prompts (isMeta envelopes, image + text
    blocks). When the same text recurs ("continue"), the occurrence nearest the log ts wins,
    and a nearest match more than ``_PROMPT_MAX_SKEW_S`` away is another turn → -1. Without
    timestamps (some hosts), the most recent occurrence wins. Not finding the prompt is
    reported honestly as -1: the caller must not attribute against the whole session.
    """
    needle = _normalized(query)[:_PROMPT_MATCH_CHARS]
    if not needle:
        return -1
    hits = [
        t
        for t in turns
        if t.role == "user" and t.kind != "summary" and needle in _normalized(t.text)
    ]
    if not hits:
        return -1
    logged = _parse_ts(ts)
    if logged is not None:
        timed = [
            (abs((when - logged).total_seconds()), t)
            for t in hits
            if (when := _parse_ts(t.ts)) is not None
        ]
        if timed:
            skew, best = min(timed, key=lambda pair: pair[0])
            return best.index if skew <= _PROMPT_MAX_SKEW_S else -1
    return hits[-1].index


def _response(turns: list[Turn], prompt_turn: int) -> list[Turn]:
    """The assistant turns answering ``prompt_turn``: up to the next real user prompt."""
    out: list[Turn] = []
    for turn in turns[prompt_turn + 1 :]:
        if turn.role == "user" and not turn.kind:
            break
        if turn.role == "assistant":
            out.append(turn)
    return out


def _next_prompt(turns: list[Turn], prompt_turn: int) -> Turn | None:
    """The user's next real prompt after ``prompt_turn`` — never a summary or host wrapper."""
    return next((t for t in turns[prompt_turn + 1 :] if t.role == "user" and not t.kind), None)


# --------------------------------------------------------------------------------------------
# Ledger store
# --------------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    session_id  TEXT NOT NULL,
    line_no     INTEGER NOT NULL,           -- turn key: record ts in epoch µs (legacy: line index)
    memory_id   TEXT NOT NULL,
    query       TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL DEFAULT 0,
    injected    INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    ts          TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL DEFAULT '',   -- '', referenced, ignored, contradicted, unknown
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

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


class Ledger:
    def __init__(self, path: Path, timeout: float = 5.0) -> None:
        """``timeout`` bounds lock waits; the per-prompt ranking read passes a short one so a
        concurrent Stop-hook write can never stall the user's turn (L1/L2)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), timeout=timeout, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            # WAL: the hot-path read never waits behind the Stop hook's attribution write.
            self._conn.execute("PRAGMA journal_mode = WAL")
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

        (``candidates.line_no`` is keyed on each record's ts, not its line index, so rotation
        cannot make a new turn collide with an old one.)
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
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO candidates "
                        "(session_id, line_no, memory_id, query, score, injected, tokens, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            _turn_key(str(record.get("ts", "")), line_no),
                            mem_id,
                            # Only injected rows are ever attributed (the one reader of the
                            # prompt); the other ~9 pool rows per turn kept a 500-char copy of
                            # it each — 75% of query bytes, multiplied again by every backup.
                            str(record.get("query", "")) if injected else "",
                            score,
                            injected,
                            tokens,
                            str(record.get("ts", "")),
                        ),
                    )
                    added += cur.rowcount
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
        report = {"referenced": 0, "ignored": 0, "contradicted": 0, "unknown": 0}
        now = _now_iso()
        led = config.ledger

        with self._lock, self._conn:
            # 1) fresh injections → referenced | ignored (| unknown when the prompt is absent)
            pending = self._conn.execute(
                "SELECT rowid, * FROM candidates "
                "WHERE session_id = ? AND injected = 1 AND outcome = ''",
                (session_id,),
            ).fetchall()
            located: dict[tuple[str, str], int] = {}
            for row in pending:
                key = (str(row["query"]), str(row["ts"]))
                if key not in located:
                    located[key] = _find_prompt_turn(turns, *key)
                prompt_turn = located[key]
                if prompt_turn < 0:
                    # No prompt, no response to judge. Attributing against the whole session
                    # instead marked ~91% of these "referenced" and fed the boost loop.
                    self._conn.execute(
                        "UPDATE candidates SET outcome = 'unknown', prompt_turn = -1, "
                        "contra_done = 1 WHERE rowid = ?",
                        (row["rowid"],),
                    )
                    report["unknown"] += 1
                    continue
                text = texts.get(row["memory_id"], "")
                tokens = distinctive_tokens(text) if text else set()
                response = " ".join(t.text for t in _response(turns, prompt_turn)).lower()
                assistant_tokens = set(_WORD_RE.findall(response))
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

            # 2) retroactive contradiction check: referenced + the user's next prompt exists
            patterns = [re.compile(p, re.IGNORECASE) for p in led.correction_patterns]
            referenced = self._conn.execute(
                "SELECT rowid, * FROM candidates "
                "WHERE session_id = ? AND outcome = 'referenced' AND contra_done = 0",
                (session_id,),
            ).fetchall()
            for row in referenced:
                prompt_turn = int(row["prompt_turn"])
                if prompt_turn < 0:  # legacy row with no located prompt: nothing to anchor to
                    self._conn.execute(
                        "UPDATE candidates SET contra_done = 1 WHERE rowid = ?", (row["rowid"],)
                    )
                    continue
                next_user = _next_prompt(turns, prompt_turn)
                if next_user is None:
                    continue  # no later prompt yet — re-check on a future Stop
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

    def trim_candidate_queries(self) -> int:
        """Blank the prompt copy on never-injected candidate rows (nothing reads it). Naturally
        idempotent; the space comes back at the next VACUUM. Returns rows trimmed."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE candidates SET query = '' WHERE injected = 0 AND query != ''"
            )
        return int(cur.rowcount)

    def repair_unlocated(self) -> int:
        """One-time repair (ledger meta ``repair_unlocated_v1``): back out of ``stats`` every
        attribution made without a located prompt (``prompt_turn = -1``) and mark those rows
        ``unknown``. Before 0.9.4 such rows were judged against the WHOLE session, which made
        ~91% of them "referenced" and inflated the boosts of long, generic memories.

        Only rows still in the candidates window can be backed out; returns how many were.
        """
        with self._lock, self._conn:
            if self._conn.execute(
                "SELECT 1 FROM meta WHERE key = 'repair_unlocated_v1'"
            ).fetchone():
                return 0
            rows = self._conn.execute(
                "SELECT rowid, memory_id, outcome, tokens FROM candidates "
                "WHERE injected = 1 AND prompt_turn = -1 "
                "AND outcome IN ('referenced', 'ignored', 'contradicted')"
            ).fetchall()
            for row in rows:
                column = str(row["outcome"])  # a contradiction already moved referenced → it
                self._conn.execute(
                    f"UPDATE stats SET injected = MAX(injected - 1, 0), "
                    f"{column} = MAX({column} - 1, 0), tokens = MAX(tokens - ?, 0) "
                    "WHERE memory_id = ?",
                    (int(row["tokens"]), row["memory_id"]),
                )
                self._conn.execute(
                    "UPDATE candidates SET outcome = 'unknown', contra_done = 1 WHERE rowid = ?",
                    (row["rowid"],),
                )
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('repair_unlocated_v1', ?)",
                (f"{len(rows)} @ {_now_iso()}",),
            )
        return len(rows)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
