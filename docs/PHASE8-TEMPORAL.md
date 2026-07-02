# Phase 8 — Bitemporal Recall + Signed Provenance

> "What did we believe on June 1 — and can we prove nobody rewrote it since?"
> The store becomes append-only and versioned; every write joins a tamper-evident hash chain.

## Invariants (extend HANDOVER §2 and PHASE6 L1–L5)

**T1 — Append-only beliefs.** A memory version is never destroyed by the write path.
Superseding sets `superseded_at`; the old version stays queryable forever. (`maintain`
backups remain the deletion-of-last-resort story.)

**T2 — Transaction-time as-of is exact; valid-time is advisory.** `--as-of T` answers
"which versions were live in the store at T" (`recorded_at <= T < superseded_at`), which is
the incident-RCA question and is computed from timestamps the system itself wrote.
`valid_from` (when the fact became true in the world) defaults to `recorded_at` and may be
overridden via metadata — it is claim-quality data and is never used to filter silently.
Calling this "bitemporal-lite" in docs is deliberate honesty.

**T3 — Supersession only on specific sources.** A new text arriving with the *same specific
source* (e.g. `native:<project>/<file>`, `session:*`) supersedes that source's live version
and inherits its `lineage`. Generic buckets (`mcp:remember`, empty) never supersede — two
"remember" calls are two independent facts. Re-storing a superseded version's exact text
*revives* it (clears `superseded_at`).

**T4 — The hot path only sees live versions.** Hook Tier-2, MCP `recall` (without `as_of`),
manifest, and `health.count` all filter `superseded_at IS NULL`. As-of queries are an
explicit, off-hot-path affordance (CLI / MCP param). The ledger keys outcomes by version id,
so outcome evidence stays attached to the exact text that was injected.

**T5 — Provenance is tamper-EVIDENT, not tamper-proof.** Every write appends an event
(`stored | superseded | revived | restated`) to a hash chain:
`entry_hash = HMAC-SHA256(key, prev_hash ‖ canonical(event))`, key at `~/.sup-mem/key`
(0600, auto-created). `sup-mem verify` re-walks the chain AND cross-checks each live row's
`sha256(text ‖ metadata)` against its latest event — a direct SQLite edit of a memory is
caught even though the row itself carries no signature. Threat model: an attacker who can
read the key can re-chain; that is stated in docs, not hidden. Origin fields (source,
session, speaker) are best-available claims whose *integrity since recording* is what the
chain guarantees.

**T6 — Backend scope.** v1 implements versioning + provenance on the default `sqlite_fts`
backend. Qdrant rejects `as_of` with a clear error instead of silently returning current
results.

## Surfaces

- `sup-mem recall "query" [--as-of 2026-06-01|ISO] [-k N] [--diff-now]` — new CLI command.
  Date-only `--as-of` means end-of-day UTC. `--diff-now` shows, per as-of hit, the live
  version of the same lineage (`unchanged | changed | retired`).
- MCP `recall` gains optional `as_of` (same semantics; description updated).
- `sup-mem verify [--quiet]` — chain + row-hash verification; non-zero exit and the first
  broken seq on failure. Also runs as a `maintain` step (failure → desktop notification).

## Migration

`user_version` 0/1 → 2 on first open: add `lineage` (=id), `valid_from` (=created_at),
rename `created_at`→`recorded_at`, add `superseded_at` (NULL), rebuild FTS. Idempotent;
existing ids, ledger stats, and backups are unaffected. Provenance chains begin at
migration time — pre-existing rows get a genesis `stored` event so verify covers them.

## Acceptance

1. Same-source re-store supersedes (old queryable via as-of, new is live, lineage shared);
   generic source does not; exact re-store revives.
2. `--as-of` before a supersession returns the OLD text; after, the new; before the first
   store, nothing. Date-only parsing = end-of-day UTC.
3. `--diff-now` labels unchanged/changed/retired correctly.
4. Hot path (hook/recall/manifest/health) sees live versions only; ledger `fetch` still
   resolves superseded ids (outcome attribution of old injections keeps working).
5. v1 database opens, migrates, and passes 1–4; re-open is a no-op.
6. `verify` green on an untouched store; a raw SQLite `UPDATE memories SET text=...` is
   detected with the offending memory named; chain edit (deleted event) detected at seq.
7. Qdrant: `as_of` raises a clear error; all pre-existing conformance tests unchanged.
8. mypy strict on darwin+linux; full suite green.
