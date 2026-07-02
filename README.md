# sup-mem

[![CI](https://github.com/kiraa06/sup-mem/actions/workflows/ci.yml/badge.svg)](https://github.com/kiraa06/sup-mem/actions/workflows/ci.yml)

**A self-hosted, pluggable global memory layer for Claude that persists context across sessions — model-free and Docker-free by default, with ultra-fast per-turn retrieval.**

![sup-mem demo: status, automatic per-prompt injection, bitemporal --as-of recall, provenance verify](docs/demo.gif)

<sub>20 seconds, real binaries: `status` (all wired) → the hook injecting memory into a prompt → `recall --as-of --diff-now` (what we believed *then* vs now) → `verify` (tamper-evident chain). Re-render with `vhs docs/demo.tape` against a seeded demo store.</sub>

---

## Install

Pick the row that matches your scale. **Most people should start with the default.**

<table>
<tr><th>Default — SQLite FTS (no Docker, no model)</th><th>Scale — vector search (opt-in)</th></tr>
<tr valign="top"><td>

```bash
curl -LsSf https://raw.githubusercontent.com/\
kiraa06/sup-mem/main/install.sh | sh
```

Zero services, zero models. Keyword (BM25) retrieval in a single embedded SQLite file. Ready in seconds.

</td><td>

```bash
# after the default install:
sup-mem setup --backend qdrant --yes
```

Brings up Qdrant (Docker), auto-detects a local embedder, ranks **by meaning**. One line, non-interactive.

</td></tr>
</table>

> **Which one?** Use the **FTS default until you have ~10k+ memories** (or you specifically want semantic/paraphrase matching). Then switch with `sup-mem setup --backend qdrant --yes`. Your `remember`/`recall` habits don't change — only the backend does.

Then **restart Claude Code** so it loads the new hook + MCP server.

---

## The loop — memories with a P&L (what makes sup-mem different)

Every other memory layer is open-loop: store → retrieve → inject → *hope*. sup-mem **closes
the loop**. A `Stop` hook reads the session transcript after each response and attributes
every injected memory as **referenced** (the answer actually used it), **ignored**, or
**contradicted** (you pushed back right after). From that evidence:

- **Reinforced retrieval** — referenced memories get a bounded score boost; repeatedly
  contradicted ones are quarantined (never deleted, always reversible).
- **`sup-mem tune`** — counterfactually replays your logged turns at other thresholds and
  recommends one with evidence ("at 0.55 you'd cut 78% of ignored injections and lose zero
  referenced ones"). `--apply` writes it to config. Honest by design: candidates that were
  never injected are reported as *unknown*, not guessed.
- **`sup-mem roi`** — token P&L per memory: what each one costs in context vs. what it
  contributes. Find the memory that burned 40k tokens this month and was never used.

All of it is advisory, fail-open, and off the hot path — the per-prompt cost is one indexed
SQLite lookup. Spec: [docs/PHASE6-LOOP.md](docs/PHASE6-LOOP.md).

**Install-and-forget:** `sup-mem service install` schedules daily housekeeping (log rotation,
store backups with retention, native-memory sweep, *lossless-only* auto-tune, provenance
verification, health check with a desktop notification on failure) — launchd on macOS, a
systemd user timer on Linux. `sup-mem status` shows the whole wiring at a glance.

**Bounded by design:** two size caps (`[archival]` in config) keep the store honest forever.
Over the main cap, the *most-useless* memories (ledger-proven: injected repeatedly, never
referenced) move to a cold `archive.db` — still reachable by `--as-of`, `restore`, and the
ledger. Over the archive cap, the oldest archived are deleted forever, FIFO, each deletion
recorded in the provenance chain. Evidence decides, never age; `keep`-tagged memories are
untouchable. Spec: [docs/PHASE9-ARCHIVAL.md](docs/PHASE9-ARCHIVAL.md).

**Time travel + tamper evidence:** the store is append-only and versioned — a changed fact
*supersedes* its predecessor instead of overwriting it, so
`sup-mem recall --as-of 2026-06-01 "pod restart cause" --diff-now` answers *"what did we
believe then, and how does it differ from now?"* — built for incident RCA. Every write also
joins an HMAC hash chain (`~/.sup-mem/key`); `sup-mem verify` proves nothing edited your
memories behind sup-mem's back (tamper-*evident*, not tamper-proof — an attacker holding the
key can re-chain; see [docs/PHASE8-TEMPORAL.md](docs/PHASE8-TEMPORAL.md)).

---

## How it works — two front-doors, one backend

Retrieval happens two ways over the **same** store: an automatic hook injects context every turn, and MCP tools let Claude read/write explicitly.

```
Per prompt (READ, synchronous, latency-critical):
  UserPromptSubmit hook (short-lived) ──► Tier 0: pinned facts (always)
                                      ──► Tier 1: skip trivial turns (greetings/thanks)
                                      ──► Tier 2: backend.search(query, k, threshold)
                                              │   (embedded DB, or a thin call to a WARM service)
                                              ▼
                                      stdout ─► injected into Claude's context

Session start:
  SessionStart hook ──► manifest() ─► compact, scale-aware topic index injected

Explicit (WRITE + fallback READ, Claude-initiated):
  MCP server ──► remember(text, …)  → backend.store(…)
             ──► recall(query, k)    → backend.search(…)   [fallback only]

Shared backend (one of):
  SqliteFtsBackend  (default: embedded file, BM25, no model, no Docker)
  QdrantBackend     (opt-in: vector kNN, auto-detected embedder)
  PgVectorBackend   (stub — same interface)
```

The hook is a **short-lived process that never loads a model**: on the FTS path it just opens the DB file; on the vector path it makes a thin call to a warm embedder (Ollama/TEI/hosted). If the only embedder is in-process (fastembed), the hook skips automatic retrieval — the MCP `recall` tool still works from the warm server.

### Registration in Claude Code

`init`/`setup` merge — **never clobber** — your Claude Code config:

- **Hooks** → `~/.claude/settings.json` under `hooks` (a `UserPromptSubmit` entry and a `SessionStart` entry). Your existing hooks and other keys are preserved; re-running is idempotent, and the prior file is backed up as `settings.json.sup-mem.bak`.
- **MCP server** → registered via `claude mcp add --scope user` into `~/.claude.json` under `mcpServers` (falls back to a direct, non-clobbering merge if the `claude` CLI isn't on PATH).

Claude Code does not hot-reload config — **restart** after `init`/`setup`. Honors `CLAUDE_CONFIG_DIR`.

The two MCP tools (their descriptions are the control surface Claude decides from):

| Tool | When Claude calls it |
|---|---|
| `remember` | The user states a durable fact/decision/preference/correction ("remember that…", "we decided…", "going forward, always…"). |
| `recall` | **Fallback** only — the user references prior work not already in the auto-injected context ("the fix we did", "that ticket"). |

---

> ## ⚠️ Model-consistency contract (vector backends)
>
> **The embedding model that *wrote* the store and the one that *reads* it must be identical.** Vectors from different models are not comparable — mixing them silently returns garbage.
>
> sup-mem records `(provider, model, dim)` inside the store and **enforces** it: `sup-mem doctor` **exits non-zero** on a mismatch, and `store`/`search` refuse rather than corrupt results. If you change models, run **`sup-mem reindex`** to re-embed everything with the new one. (Not applicable to the SQLite FTS default — it uses no model.)

---

## Tuning

Retrieval quality is a **dial you tune on your own data** — and we give you the data to tune with.

- **`retrieval.threshold`** (0..1, default `0.35`): raise to inject less (higher precision), lower to inject more (higher recall).
- **`retrieval.k`** (default `6`): max memories injected/returned per turn.
- **Retrieval log** (on by default): every turn appends `(query, injected_ids, scores, tier)` to `~/.sup-mem/retrieval.jsonl`. Eyeball it to see what's being injected and where to set the threshold. Turn off with `logging.retrieval_log = false`.
- **Tier-1 skip/cue patterns**: fully configurable regex lists — the skip-gate short-circuits trivial turns but never decides whether a *relevant* memory exists.

Everything lives in `~/.sup-mem/config.toml` (written on `init`, every knob documented). Inspect the topic index anytime with `sup-mem manifest`; check health with `sup-mem doctor`.

## Backends

| | SQLite FTS *(default)* | Qdrant *(opt-in)* | pgvector *(stub)* |
|---|---|---|---|
| Setup cost | **none** — one file, no deps | Docker + auto-detected embedder | Postgres + `pgvector` |
| Ranks by | keywords (BM25) | **meaning** (vector kNN) | meaning (vector kNN) |
| Best for | up to ~10k memories | 10k+ / semantic recall | existing Postgres shops |
| Model required | no | yes (local or hosted) | yes |
| Command | `sup-mem init` | `sup-mem setup --backend qdrant` | _interface reserved_ |

**Embedders** (auto-detected by priority on `setup`, or pin one): Ollama · fastembed (local ONNX, CPU) · TEI · Voyage · OpenAI. Hosted options warn (network + cost + data leaves your box).

## Designed for optimization

Nothing operational is hard-coded — it's all in `config.toml`:

- **Retrieval:** `retrieval.k`, `retrieval.threshold`
- **Tier-1 gate:** `tier1.skip_patterns`, `tier1.cue_patterns`
- **Manifest at scale:** `manifest.full_below`, `manifest.token_budget`, `manifest.max_topics`
- **FTS:** `fts.squash_midpoint`, `fts.squash_steepness` (the BM25→0..1 knobs)
- **Qdrant:** `qdrant.hnsw.m` / `ef_construct` / `ef`, `qdrant.quantization`
- **Embedding:** `embedding.provider`, `embedding.model`

Latency budgets it's built to: Tier-1 skip < 5 ms · FTS query (10k) < 10 ms · warm vector query < 50 ms · cached manifest < 5 ms.

## CLI

| Command | Does |
|---|---|
| `sup-mem init` | Create the SQLite FTS store, write config + pinned-facts, register with Claude Code. |
| `sup-mem setup --backend qdrant [--yes]` | Bring up Qdrant, detect the embedder, create the collection, register. |
| `sup-mem migrate-native [--dry-run]` | Copy Claude Code's built-in file memories (`~/.claude/projects/*/memory`) into the store. Copy-only, idempotent — re-run anytime to pick up stragglers. |
| `sup-mem tune [--apply]` | Counterfactual threshold replay against recorded outcomes; recommends (and optionally applies) a better threshold. |
| `sup-mem roi` | Token P&L per memory: injections, tokens, referenced/ignored/contradicted, verdicts. |
| `sup-mem recall "q" [--as-of WHEN] [--diff-now]` | Search from the CLI; `--as-of` returns what the store believed at that instant (bitemporal, sqlite backend). |
| `sup-mem verify` | Verify the tamper-evident provenance chain + row hashes; non-zero on any break. |
| `sup-mem archive [--dry-run\|--list]` | Evidence-based cold tier: superseded/quarantined move on schedule; over `main_max_mb` the most-useless decayed memories move; over `archive_max_mb` the oldest archived are **deleted forever** (FIFO, chain-audited; `0` disables). |
| `sup-mem restore <id…>` | Move archived versions back to the hot store, state intact. |
| `sup-mem status` | One-glance wiring check: hooks, MCP server, store, ledger activity, backups, service — with a fix command per red line. |
| `sup-mem maintain` | Housekeeping: rotate the retrieval log (ledger cursors rebased), back up + vacuum the stores, sweep native memories, lossless auto-tune, health check. |
| `sup-mem service install` | Schedule `maintain` daily — macOS LaunchAgent or Linux systemd user timer, auto-detected (uninstall/status too). No scheduler available: prints the crontab line. |
| `sup-mem doctor` | Backend/service health; enforce the model-consistency contract. |
| `sup-mem reindex` | Re-embed the store with the current model (vector backends). |
| `sup-mem serve` | Run the long-lived MCP server. |
| `sup-mem manifest` | Print/refresh the topic manifest. |

## Development

```bash
uv sync                 # base (SQLite FTS) dev env
uv run pytest -q        # fast suite
uv run ruff check . && uv run mypy
uv sync --extra qdrant  # add the vector path; run Qdrant via docker-compose.qdrant.yml
QDRANT_URL=http://localhost:6333 uv run pytest -q -m qdrant
```

See [HANDOVER.md](HANDOVER.md) for the full design contract and invariants.

## License

MIT — see [LICENSE](LICENSE).
