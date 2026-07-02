# Phase 10 — PreCompact Capture (the compaction lifeboat)

> Compaction is the moment context dies. Just before it does, a headless Claude reads the
> session and decides what deserves to survive — into sup-mem, where the very next prompt's
> hook can inject it back.

## Invariants

**C1 — Claude decides, sup-mem stores.** The `PreCompact` hook pipes the transcript tail
into headless `claude -p` (cheap model, `capture.model`, default `haiku`) with a strict
extraction prompt; the returned JSON facts are stored verbatim. No lexical heuristics
pretending to be judgment.

**C2 — Fail-open, always.** Missing `claude` binary, timeout (`capture.timeout_seconds`),
garbage output, unreadable transcript → store nothing, exit 0, never delay compaction more
than the configured timeout. The hook is registered with an explicit settings timeout.

**C3 — Re-compaction supersedes, never duplicates.** Each fact is stored with source
`session:<session_id>:<topic-slug>` — a *specific* source — so a later compaction's fresher
extraction of the same topic supersedes the stale one (T3 semantics), while distinct topics
coexist. Batch-internal topic collisions get `-2`, `-3` suffixes.

**C4 — No recursion.** The headless `claude -p` child runs with `SUP_MEM_CAPTURE=1`; every
sup-mem hook exits immediately when that marker is set, so the capture session cannot
trigger capture (or pay for injection it doesn't need).

**C5 — Honest about cost.** This feature spends tokens (one small-model call per
compaction — rare events). Default on, loudly documented, `capture.enabled = false` to opt
out; skipped silently when the `claude` CLI is absent. Captured memories are tagged
`auto-capture` and enter the outcome loop like everything else — the ledger will reveal
whether they earn their keep, and quarantine/archival handles the ones that don't.

## Mechanics

stdin: `{session_id, transcript_path, trigger}`. Render the last `capture.max_transcript_chars`
of main-chain turns (sidechains skipped, per-turn cap) as `USER:/ASSISTANT:` text; pipe into
`claude -p <extraction prompt> --model <capture.model>`; parse a strict JSON array of at most
`capture.max_memories` `{text, topic, tags}` objects (code-fence tolerant); store each with
tags + `auto-capture`; append one line to `~/.sup-mem/logs/capture.log` for observability.

## Acceptance (hermetic — a fake `claude` script; CI never spends tokens)

1. Canned JSON → facts stored with topic-keyed sources; re-run with a changed fact on the
   same topic supersedes (live count stable, versions grow).
2. `SUP_MEM_CAPTURE=1` → all four hooks exit 0 doing nothing.
3. Fake `claude` exiting non-zero / emitting prose / absent from PATH → exit 0, nothing stored.
4. `max_memories` cap enforced; `enabled=false` short-circuits; transcript rendering uses
   main-chain turns only and respects the char budget.
5. Registration includes the PreCompact hook (with timeout); status covers it automatically.
