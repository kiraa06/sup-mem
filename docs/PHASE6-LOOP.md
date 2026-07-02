# Phase 6 — The Outcome Loop

> sup-mem becomes the first memory layer that **measures whether its memories actually
> helped** and optimizes itself on that evidence. Everything else in the field is open-loop:
> store → retrieve → inject → *hope*. We close the loop with signals no one else collects.

## Why this is possible here and nowhere else

Claude Code's `Stop` hook fires after every completed response and hands us
`transcript_path` — the full session JSONL. Combined with the retrieval log we already write
per turn (`query`, candidates, scores, what was injected), the "did it help?" signal is
sitting on local disk. No cloud memory vendor sees the downstream conversation; we do.

## Invariants (extend §2 of HANDOVER.md)

**L1 — Attribution is off the hot path.** All transcript analysis happens in the `Stop`
hook (fires after the response is complete). The per-prompt hook adds at most one indexed
SQLite lookup (ledger stats for the candidate ids). Budgets: Stop hook < 500 ms on a 5 MB
transcript; per-prompt ledger lookup < 2 ms.

**L2 — The ledger is advisory and fail-open.** A missing, locked, or corrupt `ledger.db`
must never break retrieval, the hooks, or the MCP tools. Every ledger read/write is wrapped;
on any error the system behaves exactly as pre-Phase-6.

**L3 — Evidence-conservative.** Nothing is ever hard-deleted by the loop. Utility boosts are
bounded (±`ledger.boost_weight`, default 0.10) so base relevance always dominates.
Quarantine requires repeated contradictions (`ledger.quarantine_contradictions`, default 3,
and more contradictions than references) and is reversible by editing/clearing the ledger.

**L4 — Honest counterfactuals.** `tune` only claims outcomes for injections that actually
happened. Candidates that were logged below the live threshold have *unknown* outcomes and
are reported as such — never counted as wins or losses.

**L5 — Backend-agnostic.** The ledger lives in its own SQLite file (`~/.sup-mem/ledger.db`)
keyed by memory id, independent of which `MemoryBackend` stores the memories. The only
backend addition is `fetch(ids) -> {id: text}`.

## Mechanics

### Signal collection (per prompt, existing hook)
Tier-2 now searches a wider candidate pool (`ledger.pool_k`, default 12) at threshold 0,
applies the utility adjustment (below), injects the top `retrieval.k` above
`retrieval.threshold`, and logs **all** candidates with `(id, score, injected, est_tokens)`
plus `session_id` to `retrieval.jsonl`.

### Attribution (per response, new Stop hook)
`sup-mem-hook-stop` reads the new retrieval-log lines for this session (per-session cursor),
parses the transcript (main-chain `user`/`assistant` entries only — sidechains are subagent
noise), and for each **injected** memory:

- **referenced** — the assistant text after the prompt contains enough of the memory's
  distinctive tokens: `matched >= max(min_overlap_tokens, ceil(overlap_fraction * |tokens|))`.
- **ignored** — otherwise.
- **contradicted** — a referenced memory whose *next user turn* matches a correction pattern
  (`ledger.correction_patterns`). Checked retroactively on later Stops; flips referenced →
  contradicted once.

Counters accumulate per memory in `ledger.db` (`injected/referenced/ignored/contradicted`,
token totals, timestamps). Deduped by (session, log line) cursor — re-running is safe.

### Reinforced retrieval (ranking.py)
`score' = clamp01(score + boost_weight * (referenced − contradicted) / max(injected, 3))`,
then quarantine-drop (L3 rule), then re-sort. Applied in the hook's Tier-2 and MCP `recall`.
Disabled entirely by `ledger.enabled = false`.

### `sup-mem tune` — counterfactual threshold replay
Replays every logged turn at a grid of thresholds against recorded outcomes:
injections/turn, referenced kept/lost, ignored avoided, unknown added (L4), est. tokens per
turn. Recommends the highest threshold that loses zero referenced injections. `--apply`
writes `retrieval.threshold` into `config.toml`.

### `sup-mem roi` — token P&L per memory
Per memory: injections, tokens consumed, referenced/ignored/contradicted, verdict
(valuable / wasteful / quarantined / watching), plus store-wide totals.

## Acceptance

1. Attribution: seeded transcript + log → expected referenced/ignored/contradicted; second
   run adds nothing (cursor); sidechain entries ignored; garbage transcript → exit 0.
2. Ranking: referenced memory outranks ignored at equal base score; quarantined dropped;
   `ledger.enabled=false` is a byte-identical passthrough; scores stay in 0..1.
3. Tune: grid math correct on synthetic data; recommendation loses zero referenced;
   unknowns reported separately; `--apply` round-trips through config.
4. ROI: totals equal ledger sums.
5. Backend conformance: `fetch()` roundtrips on sqlite **and** qdrant.
6. Hot path: §11.2 lazy-import test still green; Tier-1 path untouched.
7. Fail-open: ledger deleted mid-session → hook + recall still work (L2).
