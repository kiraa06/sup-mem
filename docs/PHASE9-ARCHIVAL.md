# Phase 9 — Evidence-Based Archival with Size-Bounded Tiers

> Memories that stop earning their keep move to a cold tier; if even the cold tier fills,
> the oldest archived memories are deleted forever, FIFO. Both caps are user-defined.

## Amendment to T1/L3 — stated, not smuggled

Phases 6–8 promised "never hard-delete." Phase 9 amends that **by explicit user policy**:
permanent deletion happens in exactly one place — FIFO purge of the *archive* tier when it
exceeds `archival.archive_max_mb` — and every purge is recorded as a `purged` event in the
provenance chain (id, lineage, final content hash, topic), so the *fact and identity* of a
deletion remain auditable after the content is gone. Nothing in the hot store is ever
deleted directly; content must pass through the archive tier first.

## Invariants

**A1 — Evidence, never age.** Eligibility comes from structure and the outcome ledger:
- *superseded* versions older than `superseded_after_days` (structural certainty),
- *quarantined* memories stale for `quarantined_after_days` (proven harmful),
- *decayed* memories under size pressure only: injected but never referenced (proven
  useless), ranked most-useless-first.
A memory referenced recently is never archived by pressure; `keep_tag` ("keep") is a hard
opt-out. Calendar age alone never qualifies anything.

**A2 — Two caps, three regimes** (`archival.main_max_mb`, `archival.archive_max_mb`):
1. *Steady state*: only the structural tiers (superseded/quarantined) move, cap or no cap.
2. *Main pressure* (main DB > cap): decay-tier candidates move most-useless-first, batch →
   VACUUM → re-measure, until under the cap or candidates are exhausted. If exhausted while
   still over cap, STOP and report — sup-mem does not archive evidently-useful memories to
   satisfy a quota; the fix (raise the cap) is the user's call.
3. *Archive pressure* (archive DB > cap): delete forever, FIFO by `archived_at`, batch →
   VACUUM → re-measure, until under the cap.

**A3 — Cold, not gone (until purged).** Archived versions leave the hot table and the FTS
index (the entire point: smaller candidate pool, faster Tier-2) but remain first-class for:
- `--as-of` recall (T2's exactness survives — temporal queries UNION the archive,
  coverage-scored; documented as O(archive window) on the cold tier),
- `fetch()` (ledger attributions of old injections keep resolving),
- `sup-mem restore <id>` (moves a version back, superseded/live state intact).

**A4 — The chain follows the memory.** New provenance events: `archived`, `restored`,
`purged`. `verify` becomes a location-aware state machine: each id's latest content event
fixes its expected hash, and its latest location event fixes where that hash must be found
(main / archive / nowhere). Tampering with an archived row is caught exactly like a live one.

**A5 — Everything tunable; deletion opt-out-able.** All knobs in `[archival]`; setting
`archive_max_mb = 0` disables purging entirely (archive grows unbounded); `enabled = false`
disables the whole subsystem.

**A6 — Sizes are honest.** "Size" = the SQLite file after checkpoint+VACUUM, not logical
row bytes; pressure loops re-measure after each batch's VACUUM so a cap means what a user
thinks it means (bytes on disk).

## Surfaces

- `sup-mem archive [--dry-run | --list]` — run all three regimes / preview / list the
  archive tier (id, topic, archived_at, size).
- `sup-mem restore <id> [<id>…]` — move versions back to the hot store.
- `maintain` gains an `archival` step (after backup — pre-archival state is always in the
  latest snapshot; before vacuum/provenance).
- `status` shows both tier sizes against their caps.

## Uselessness ranking (main pressure)

Exclude: `keep`-tagged, referenced within `decay_min_age_days`, younger than
`decay_min_age_days`. Rank the rest ascending by
`(referenced_total, -ignored, -injected, recorded_at)` — i.e. never-referenced first, most
ignored-despite-chances first among those, oldest first as the tiebreak. Memories with no
ledger evidence rank after evidenced-useless ones (no evidence ≠ proven useless).

## Acceptance

1. Steady state: old superseded + stale quarantined move; recent superseded stays; as-of
   still finds archived versions; fetch resolves them; verify green.
2. Restore round-trips (state intact) with chain events; verify green.
3. Main pressure: with a tiny cap, most-useless move first, evidently-useful and
   `keep`-tagged never move, loop stops under cap or reports exhaustion.
4. Archive pressure: FIFO purge until under cap; purged ids unresolvable; `purged` chain
   events present; verify green afterward.
5. Tampering with an archived row fails verify.
6. Dry-run moves nothing; disabled config short-circuits; maintain step ordering correct.
7. mypy strict darwin+linux; full suite green.
