# Launch kit (drafts — edit voice to taste, then delete this file or keep as reference)

## Show HN

**Title** (≤80 chars):

> Show HN: Sup-mem – memory for Claude Code that measures if memories helped

**Body:**

I built a self-hosted memory layer for Claude Code with one idea the existing tools
(mem0, Zep, Letta, native memory) don't have: **close the loop**.

Every memory system stores and retrieves. None of them know what happened *after* the
memory got injected. Sup-mem does, because Claude Code's `Stop` hook hands you the full
session transcript: after every response, it scores each injected memory as *referenced*
(the answer actually used it), *ignored*, or *contradicted* (you pushed back right after).
From that evidence:

- `sup-mem roi` — a token P&L per memory ("this memory cost 41k context tokens this month,
  was used twice, contradicted once")
- `sup-mem tune` — counterfactual replay of your own logged turns at other thresholds;
  applies a new one only when it's lossless
- reinforcement + quarantine — memories that keep getting contradicted stop being injected

It caught itself in the act during development: 19% of injected memories were actually
used (~65k wasted tokens in one session). It auto-raised its own threshold (0.35→0.80,
losslessly) and a per-memory clip cut injection cost 86%. Measured, not vibes.

The rest of it, briefly: zero-infra default (SQLite FTS5 — no Docker, no embedding model,
<10 ms retrieval; opt-in Qdrant for vector search), bitemporal recall
(`sup-mem recall "pod restart cause" --as-of 2026-06-01 --diff-now` — what did we believe
*then* vs now; built for incident RCA), a tamper-evident HMAC provenance chain
(`sup-mem verify` catches a raw sqlite UPDATE of a memory), memories that survive context
compaction (PreCompact hook), evidence-based decay with hard size caps (over the archive
cap it deletes FIFO, chain-audited), and a daily self-maintenance service (launchd/systemd).

Everything is local — nothing leaves your machine unless you opt into a hosted embedder.

Install: `uv tool install sup-mem && sup-mem init` (or pipx / brew / curl one-liner).

Honest limitations: attribution is lexical (paraphrased use can score as "ignored" — it's
a conservative estimator by design), the outcome loop needs the sqlite backend, and it's
deeply Claude Code-specific — that coupling is exactly what makes the loop possible.

Repo: https://github.com/kiraa06/sup-mem

<!-- OPTIONAL origin-story paragraph — include if you're comfortable with the meta angle:
Meta-footnote: the project was built in a single day of pair-work with Claude Code itself,
phase by phase against a written spec with acceptance gates (the HANDOVER.md and
docs/PHASE*.md files in the repo are the actual contracts it was built against). The
memory layer's first user was the session that built it — its own ledger data is what
drove the threshold auto-tune above.
-->

## r/ClaudeAI

**Title:** I built a memory layer for Claude Code that knows which of its memories
actually get used (and prunes the rest)

**Body (short):**

Auto-injects relevant memories into every prompt via a UserPromptSubmit hook, then uses
the Stop hook to check the transcript: did the response actually *use* what got injected?
Every memory accumulates referenced/ignored/contradicted counts → token ROI per memory →
self-tuning retrieval threshold → quarantine for repeatedly-wrong memories → decay/archival
for dead weight. Plus `--as-of` time-travel recall and a provenance chain so nothing can
edit your memories behind your back. SQLite by default (no Docker/model), local-only.
It also snapshots durable facts right before context compaction, so long sessions stop
losing their minds.

`uv tool install sup-mem && sup-mem init` — repo: https://github.com/kiraa06/sup-mem

## X/Twitter thread skeleton

1/ Every LLM memory tool stores and retrieves. None of them know if the memory *helped*.
   I built one for Claude Code that does. 🧵
2/ [demo gif] Auto-injection per prompt → Stop-hook reads the transcript → every memory
   scored: referenced / ignored / contradicted.
3/ `sup-mem roi`: token P&L per memory. Mine found a 19% hit rate — 65k wasted tokens in
   one session. It raised its own threshold. Losslessly. On evidence.
4/ `sup-mem recall --as-of 2026-06-01 --diff-now`: what did we believe THEN vs now.
   Bitemporal memory for incident RCA.
5/ HMAC provenance chain: `sup-mem verify` catches anyone editing your memories behind
   sup-mem's back. Local-only, SQLite, no Docker, no model.
6/ `uv tool install sup-mem && sup-mem init` → github.com/kiraa06/sup-mem

## Launch-day checklist

- [ ] Post Show HN in the morning US-time window; hang around to answer comments fast
- [ ] r/ClaudeAI same day, X thread after HN gets traction (link the HN thread)
- [ ] Watch `sup-mem` PyPI download stats + GitHub traffic for the week
- [ ] Fast-respond to first issues — early responsiveness compounds
