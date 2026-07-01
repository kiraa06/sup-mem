# HANDOVER — `claude-memory`
> A self-hosted, pluggable **global memory layer for Claude** that persists context across
> sessions. Default install is model-free and Docker-free with a **one-line setup**; users
> with large stores opt into vector search. Built for **ultra-fast per-turn retrieval**.
>
> **This document is the contract.** Build to it. The numbered invariants in §2 are
> non-negotiable — if an implementation choice would violate one, stop and flag it rather
> than working around it. Everything else is an implementation detail you may improve.
---
## 0. How to use this handover (for Claude Code)
1. Read the whole file before writing any code. The invariants (§2) and anti-patterns (§9)
   encode decisions already litigated — do not re-derive them.
2. Build in the phases in §10 using subagents where marked `[parallel]`.
3. After each phase, run its acceptance checks (§11). Do not proceed on red.
4. The definition of done is: `git clone` → one command → working memory in Claude Code,
   **and** the full acceptance suite (§11) passes in CI.
---
## 1. Assumptions (change these first if wrong)
| Assumption | Value | Change it in |
|---|---|---|
| Language / runtime | Python ≥ 3.11 | `pyproject.toml` |
| Package/deps manager | `uv` (for fast, reproducible, one-line setup) | `install.sh`, `pyproject.toml` |
| Primary client | Claude Code (hooks + MCP). Claude Desktop supported for MCP only. | `cli.py` registration |
| License | MIT | `LICENSE` |
| Repo / package name | `claude-memory` | everywhere |
| Default backend | SQLite FTS5 (zero-dependency, no Docker, no model) | `config.py` |
| Default embedder (vector path) | `fastembed` + `BAAI/bge-small-en-v1.5` (CPU, ONNX, no server) | `embedding/detect.py` |
If any of these is wrong, fix the table and propagate before building.
---
## 2. Non-negotiable invariants
**I1 — Two front-doors, one backend.** Retrieval happens two ways over the *same* storage:
(a) an automatic **hook** that injects context every turn without Claude choosing to, and
(b) **MCP tools** (`remember`, `recall`) that Claude calls explicitly. They share one
`MemoryBackend`. Never build two separate stores.
**I2 — The hook is a short-lived process. It must never load a model or do heavy init.**
It is spawned fresh per prompt, lives milliseconds, and dies. It may only make thin calls to
already-warm services or open an embedded DB file. Any model, pool, or cache that costs more
than a few ms to warm belongs in a long-running process, not the hook. This is the single
most load-bearing rule for "ultra fast."
**I3 — Tiered retrieval; regex is a skip-gate, never a relevance-gate.**
- Tier 0: pinned facts, always injected (flat file, no lookup).
- Tier 1: a cheap lexical **skip** check — a *whitelist of obviously-trivial turns*
  (greetings, "thanks", self-contained one-off asks). Its only job is to short-circuit turns
  that need no episodic memory. It must **never** decide whether a relevant memory *exists*.
- Tier 2: real retrieval (FTS or vector), gated on a **relevance score threshold**, not on
  keyword presence.
**I4 — The write path is off the hot path.** Never embed-and-store synchronously while Claude
is responding. Writes happen via the explicit `remember` tool or a session-end batch. Reads
and writes share storage but must not block each other.
**I5 — Claude decides tool calls from descriptions + conversation + injected context +
manifest — never by surveying the store.** The tool descriptions are the control surface;
write them carefully (§6.5). Claude can see what the hook injected this turn and the
session-start manifest — those are its only "map" of what exists.
**I6 — Pluggable backend behind one interface (§6.1).** Everything above the interface (hook,
MCP tools, manifest) is backend-agnostic. Adding a backend must not touch them.
**I7 — Embedding-model consistency is a hard contract (vector backends).** The model that
writes and the model that reads must be identical. Store `(provider, model, dim)` in backend
metadata; refuse to start (or warn loudly + exit non-zero in `doctor`) on mismatch; ship a
`reindex` command. Vectors from different models are not comparable.
**I8 — The default experience is one command, no Docker, no model.** A person evaluating the
repo must get working memory (SQLite FTS) from a single command with zero services to spin
up. Vector search is strictly opt-in.
**I9 — Everything is tunable.** Threshold, `k`, tier-1 skip patterns, manifest strategy,
chunking, HNSW/quantization params — all live in config with sane defaults, none hard-coded
in logic. See §8.
**I10 — Manifest degrades gracefully with scale.** Full topic list when small; clustered /
summarized when large; never dump tens of thousands of tags into context.
---
## 3. Architecture (recap)
```
Per prompt (READ, synchronous, latency-critical):
  UserPromptSubmit hook (short-lived) ──► Tier 0 pinned facts (always)
                                      ──► Tier 1 skip check (trivial? exit)
                                      ──► Tier 2 backend.search(query, k, threshold)
                                              │  (thin call to WARM service or embedded DB)
                                              ▼
                                      stdout ─► injected into Claude's context
Session start:
  SessionStart hook ──► backend.manifest() ─► compact topic index injected
Explicit (WRITE + fallback READ, Claude-initiated):
  MCP server ──► remember(text, metadata)   → backend.store(...)
             ──► recall(query, k)            → backend.search(...)   [fallback only]
Shared backend (one of):
  SqliteFtsBackend  (default: embedded file, BM25, no model, no Docker)
  QdrantBackend     (opt-in: vector kNN, auto-detected embedder)
  PgVectorBackend   (optional/future, same interface)
```
Warm services (vector path only) stay up via `restart: unless-stopped`. The hook borrows
their warmth; it never generates its own (I2).
---
## 4. Repository layout
```
claude-memory/
├── README.md                      # see §12 for required sections
├── LICENSE                        # MIT
├── pyproject.toml                 # uv-managed, extras: [qdrant], [pgvector], [voyage], [openai]
├── install.sh                     # one-line installer (wraps uv)
├── docker-compose.qdrant.yml      # brought up only by `setup --backend qdrant`
├── .github/workflows/ci.yml       # lint + type + test matrix; see §11.6
├── .env.example
├── src/claude_memory/
│   ├── __init__.py
│   ├── config.py                  # load/merge config (defaults ← file ← env ← flags)
│   ├── models.py                  # Hit, MemoryRecord dataclasses
│   ├── backends/
│   │   ├── base.py                # MemoryBackend ABC (§6.1)
│   │   ├── sqlite_fts.py          # default (§6.2)
│   │   ├── qdrant.py              # opt-in (§6.3)
│   │   └── pgvector.py            # optional stub, same interface
│   ├── embedding/
│   │   ├── __init__.py
│   │   ├── detect.py              # auto-detection + fallback (§6.4)
│   │   └── providers.py           # fastembed / ollama / tei / voyage / openai
│   ├── hook/
│   │   ├── user_prompt_submit.py  # tiered router (§6.6) — the hot path
│   │   └── session_start.py       # manifest injection
│   ├── mcp/
│   │   └── server.py              # remember / recall (§6.5)
│   ├── manifest.py                # scale-aware topic index (§6.7)
│   └── cli.py                     # init / setup / doctor / reindex / serve / manifest (§7)
└── tests/
    ├── conftest.py
    ├── test_backends.py           # interface conformance, run against every backend
    ├── test_hook_tiers.py
    ├── test_embedding_detect.py
    ├── test_mcp.py
    ├── test_manifest_scale.py
    └── test_e2e.py                # clone→setup→retrieve smoke
```
---
## 5. Tech stack & rationale
- **`uv`** for install/deps — chosen specifically for the "ultra fast one-line setup"
  requirement; `uv sync` is an order of magnitude faster than pip and gives a lockfile.
- **SQLite FTS5 + BM25** (stdlib `sqlite3`, no extra dep) — the zero-friction default (I8).
- **Qdrant** (`qdrant-client`) as the opt-in vector store — small footprint, HNSW +
  quantization, clean Docker story.
- **`fastembed`** as the default local embedder — ONNX, CPU-friendly, **runs inside the
  long-lived MCP server process** so no separate model server is needed. This directly
  answers "I don't want to run a model": for the FTS default there is no model at all, and
  for the vector path the model is embedded in a process that's already running.
- **MCP Python SDK** for the server.
- Optional extras (`[voyage]`, `[openai]`) for users who prefer a hosted embedder.
---
## 6. Component specifications
### 6.1 `MemoryBackend` interface — `backends/base.py`
Everything above the line (hook, MCP, manifest) depends only on this. Adding a backend must
not require touching them (I6).
```python
from abc import ABC, abstractmethod
from claude_memory.models import Hit, MemoryRecord
class MemoryBackend(ABC):
    @abstractmethod
    def store(self, text: str, metadata: dict) -> str:
        """Persist a memory. Returns its id. Idempotent on (text, source) if possible."""
    @abstractmethod
    def search(self, query: str, k: int, threshold: float) -> list[Hit]:
        """Return up to k hits with score >= threshold, best first.
        Score MUST be normalized to 0..1 across backends so the threshold is portable."""
    @abstractmethod
    def manifest(self, max_topics: int) -> list[str]:
        """Return a compact, scale-aware topic index (see §6.7)."""
    @abstractmethod
    def health(self) -> dict:
        """Liveness + config summary: backend name, count, embed (provider, model, dim)|None."""
    @abstractmethod
    def reindex(self, progress=None) -> None:
        """Re-embed/rebuild. No-op for lexical backends; required for vector (I7)."""
```
`Hit` = `{id, text, score, metadata}`. **Score normalization to 0..1 is mandatory** so
`threshold` means the same thing regardless of backend (BM25 needs a squashing function; kNN
cosine maps naturally).
### 6.2 `SqliteFtsBackend` (default, I8)
- Single file at `~/.claude-memory/memory.db`. No server, no model, no Docker.
- FTS5 virtual table; rank with `bm25()`. Normalize BM25 → 0..1 (document the squash).
- `store()` inserts + updates the FTS index. `manifest()` returns distinct tags/topics.
- `reindex()` is a no-op (rebuild FTS if schema changed).
- Must be importable and usable with **zero optional deps installed**.
### 6.3 `QdrantBackend` (opt-in, scale)
- Collection with named vector; HNSW params and (optional) scalar quantization in config (§8).
- Embeds via the selected provider (§6.4). **Embedding happens inside the long-lived MCP
  server process, not the hook** (I2). The hook calls the backend which calls a warm embed
  endpoint / in-process fastembed.
- Persists `(provider, model, dim)` in a Qdrant payload/meta doc on first write. On startup
  and in `doctor`, compare against configured model → enforce I7.
- `reindex()` re-embeds every record with the current model, with progress callback.
### 6.4 Embedding auto-detection — `embedding/detect.py` (they asked for this explicitly)
`detect_embedding_provider(config)` resolves a provider by **priority**, prints what it found,
and (interactive) lets the user choose, or (— `--yes` / non-interactive) auto-picks the top
available. Record the choice into backend meta (I7).
```
Priority order:
 0. If config pins provider+model → validate it's reachable, use it. Respect the user.
 1. Ollama reachable (OLLAMA_HOST, default http://localhost:11434)?
      GET /api/tags, filter embedding-capable models
      (nomic-embed-text, mxbai-embed-large, all-minilm, snowflake-arctic-embed).
      If any → offer; default nomic-embed-text.
 2. fastembed importable?  → offer; default BAAI/bge-small-en-v1.5 (384-dim, CPU, no server).
 3. TEI reachable (TEI_URL)? → offer.
 4. VOYAGE_API_KEY set? → offer voyage-3 (hosted; warn: network + cost + leaves your box).
 5. OPENAI_API_KEY set? → offer text-embedding-3-small (same warnings).
 6. Fallback: install fastembed + pull bge-small-en-v1.5.
    If offline AND nothing usable → hard error with exact remediation commands.
```
Requirements:
- Detection must be **fast and side-effect-free** until a choice is made (no downloads while
  probing).
- Emit a clear table: provider | model | dim | where it runs | latency class | selected.
- Non-interactive mode is what makes the Qdrant path still "one line": it picks the best
  available automatically and records it.
- Always surface **fallbacks** in the output so the user knows the alternatives.
### 6.5 MCP server — `mcp/server.py`
Exposes exactly two tools. **The descriptions are the control surface (I5)** — Claude decides
purely from these + the conversation + injected context + manifest. Ship these strings
roughly as-is:
```
remember:
  "Store a durable fact, decision, preference, or correction that should persist across
   future sessions. Call when the user says things like 'remember that…', 'we decided…',
   'going forward, always…', or states a stable fact about their systems/preferences.
   Do NOT call for transient, turn-specific details or things already obviously stored."
recall:
  "Fallback retrieval from long-term memory. Relevant context is normally injected
   automatically each turn, so call this ONLY when: the user references prior work you lack
   context for (e.g. 'the fix we did', 'that ticket', possessives about past projects) AND
   the context already present this turn does not cover it — optionally guided by a topic
   from the session manifest. Pass a focused query."
```
- Server is long-lived (`serve`), holds the backend (and in-process fastembed if used) warm.
- Also usable from Claude Desktop via MCP config.
### 6.6 The hook — `hook/user_prompt_submit.py` (the hot path)
Implements Tiers 0–2 (I3). Reads Claude Code's `UserPromptSubmit` JSON from stdin; anything
printed to stdout is injected into context.
- **Lazy-import everything heavy.** On the Tier-1 skip path, the process must not import the
  backend, embedding libs, or an HTTP client. Import inside the Tier-2 branch only. Verify
  with the import-time test (§11.2).
- Tier 0: `cat` the pinned-facts file (fast, unconditional).
- Tier 1: compiled skip regex (whitelist) + a "never-skip" cue regex (possessives, definite
  articles, past-tense references, ticket keys). Skip only if skip-match AND no cue.
- Tier 2: `backend.search(prompt, k, threshold)`; print hits under a short header.
- Total added latency budget: §8.
- Fail open and silent: any error → print nothing, exit 0. A broken memory layer must never
  block the user's prompt.
`hook/session_start.py`: print `backend.manifest(max_topics)` under a header. Cache it
(§8) so it isn't recomputed when the store is unchanged.
### 6.7 Manifest — `manifest.py` (scale-aware, I10)
- Small store (< `manifest.full_below`, default 300): list distinct topics/tags verbatim.
- Large store: cluster/group (tag rollups, or embed-cluster centroids labeled) and emit a
  summarized index within a token budget.
- Cache keyed on store revision (max updated_at or a counter). Regenerate only on change.
---
## 7. CLI & setup UX — `cli.py`
The setup experience is a graded requirement (I8). All commands must be idempotent and
re-runnable.
| Command | Does |
|---|---|
| `claude-memory init` | **Default one-liner.** Create SQLite FTS store, write pinned-facts file, register the hook + MCP server into Claude Code settings, print next steps. No Docker, no model, no prompts. |
| `claude-memory setup --backend qdrant [--yes]` | Bring up `docker-compose.qdrant.yml`, run embedding detection (§6.4), record model, migrate/create collection, register hook + MCP. `--yes` = non-interactive, auto-pick embedder → still one line. |
| `claude-memory doctor` | Health of backend + services; **enforce I7** (exit non-zero on model mismatch) with exact fix commands. |
| `claude-memory reindex` | Re-embed the store with the current model (vector backends). Progress bar. |
| `claude-memory serve` | Run the long-lived MCP server. |
| `claude-memory manifest` | Print/refresh the manifest (debug + cache warm). |
**One-line installs** (README must show both):
```bash
# Default — FTS, no Docker, no model:
curl -LsSf https://raw.githubusercontent.com/<you>/claude-memory/main/install.sh | sh
# install.sh: ensures uv, `uv tool install claude-memory`, then `claude-memory init`
# Large-scale — vector search:
claude-memory setup --backend qdrant --yes
```
Registration must **detect existing config and merge, not clobber** the user's Claude Code
`settings.json` hooks / MCP servers.
---
## 8. Performance budget & optimization surface (I9, "ultra fast", "as optimizable as possible")
**Latency budgets (assert in tests where feasible, §11.5):**
| Path | Budget |
|---|---|
| Tier-1 skip (trivial turn), total hook overhead | < 5 ms |
| Tier-2 FTS query (10k records) | < 10 ms |
| Tier-2 vector query, warm service | < 50 ms |
| Manifest injection (cached) | < 5 ms |
**Required optimizations:**
- Lazy imports on the hook skip path (I2) — enforced by test §11.2.
- Warm services only for embedding/vectors; never in the hook.
- Manifest caching keyed on store revision.
- Batch writes; never one-embed-per-insert in a loop on the write path.
- In-process fastembed reused across MCP calls (don't re-instantiate per request).
- Qdrant: expose HNSW `m` / `ef_construct` / search `ef`, and optional scalar quantization,
  all in config.
- FTS: expose BM25 `k1`/`b` and the score-squash constants.
**Tuning knobs (config, all with defaults):**
`retrieval.k`, `retrieval.threshold`, `tier1.skip_patterns`, `tier1.cue_patterns`,
`manifest.full_below`, `manifest.token_budget`, `chunking.*`, `qdrant.hnsw.*`,
`qdrant.quantization`, `embedding.provider`, `embedding.model`.
**Retrieval logging for tuning (ship it on by default, off switch in config):** log
`(query, injected_ids, scores, tier_taken)` to a local file so users can eyeball
precision/recall and tune the threshold. This is the honest answer to "the threshold is a
dial you must tune on your own data" — give them the data to tune with.
---
## 9. Anti-patterns — do NOT do these (each maps to an invariant)
- ❌ Load an embedding model / open pools inside the hook. (I2)
- ❌ Embed synchronously on the write hot path. (I4)
- ❌ Use regex/keywords to decide whether a relevant memory *exists*. (I3)
- ❌ Make Claude survey the store to decide whether to call a tool, or expose a "list all
  memories" tool for that purpose — use descriptions + manifest. (I5)
- ❌ Mix embedding models between write and read, or switch models without `reindex`. (I7)
- ❌ Require Docker or a model for the default install. (I8)
- ❌ Hard-code threshold/k/patterns in logic instead of config. (I9)
- ❌ Dump the entire tag list into context at scale. (I10)
- ❌ Let a memory-layer error block or delay the user's prompt — fail open, silent, exit 0.
---
## 10. Build plan for agents
Run phases in order. Items marked `[parallel]` can be separate subagents; join at the phase's
acceptance gate before moving on.
**Phase 0 — Scaffold.** Repo layout (§4), `pyproject.toml` with extras, `install.sh`,
`.github/workflows/ci.yml`, `LICENSE`, `models.py`, `config.py` (defaults←file←env←flags).
Gate: `uv sync` succeeds; `claude-memory --help` runs.
**Phase 1 — Interface + default backend.** `backends/base.py`, then `sqlite_fts.py` with score
normalization. Gate: §11.1 conformance suite green against SQLite; §11.3 default-deps import
test green (no optional deps installed).
**Phase 2 `[parallel]`:**
- 2a — Hook: `user_prompt_submit.py` (tiers), `session_start.py`. Gate §11.2, §11.4.
- 2b — MCP server: `mcp/server.py` with the two tools + descriptions. Gate §11.4.
- 2c — Manifest: `manifest.py` scale-aware. Gate §11.7.
**Phase 3 — Vector backend + embedding detection `[parallel]`:**
- 3a — `embedding/providers.py` + `detect.py`. Gate §11.8 (mock each provider; detection
  priority + fallback + non-interactive).
- 3b — `qdrant.py` + `docker-compose.qdrant.yml` + I7 enforcement + `reindex`. Gate §11.1
  conformance suite green against Qdrant too.
**Phase 4 — CLI & registration.** `init`, `setup`, `doctor`, `reindex`, `serve`, `manifest`;
Claude Code settings merge (non-clobbering). Gate §11.9.
**Phase 5 — E2E, docs, polish.** `test_e2e.py`, README (§12), `.env.example`, retrieval
logging. Gate: full suite green in CI; both one-line installs verified in a clean container.
**Verification agent:** after Phases 1–5, a dedicated agent runs the *entire* §11 suite from a
clean checkout and produces a pass/fail report per criterion. Nothing ships red.
---
## 11. Acceptance criteria (the verification contract)
### 11.1 Backend conformance (run against EVERY backend)
- store→search round-trips; returned hits respect `k` and `threshold`.
- scores are within 0..1 and monotonic with relevance.
- `health()` reports count and embed meta correctly.
- `manifest(max_topics)` never exceeds `max_topics`.
### 11.2 Hook lazy-import (I2)
- With backend/embedding modules instrumented, a **trivial prompt** ("thanks") triggers the
  Tier-1 skip and imports **none** of them. Assert via `sys.modules` inspection in a
  subprocess. This test failing means the hot path is slow — treat as build-breaking.
### 11.3 Default-deps install
- In an env with only base deps (no `qdrant-client`, no `fastembed`), `import claude_memory`,
  `init`, store, and search all work.
### 11.4 Tier logic (I3)
- Greeting/thanks → skip, nothing beyond Tier 0 injected.
- Same-topic paraphrase with weak keywords but a cue ("the thing we fixed") → NOT skipped.
- Tier-2 respects the score threshold (below-threshold hits are dropped).
### 11.5 Latency budgets (§8)
- Assert Tier-1 overhead and FTS query budgets on a seeded 10k-record store. Vector budget
  asserted against a warm local Qdrant in CI (or marked `@slow` if Qdrant unavailable).
### 11.6 Fail-open
- Backend raises on `search` → hook prints nothing, exits 0, does not raise.
### 11.7 Manifest scale (I10)
- < `full_below` records → verbatim list. 50k records → summarized, within token budget,
  cached (second call does no store scan).
### 11.8 Embedding detection (§6.4)
- Priority order honored with mocked availability of each provider.
- Non-interactive `--yes` auto-picks top available.
- Nothing available + offline → hard error with remediation text.
- Chosen `(provider, model, dim)` is recorded and read back.
### 11.9 Model-consistency contract (I7)
- Writing with model A then configuring model B → `doctor` exits non-zero with a clear message;
  `reindex` fixes it; post-reindex `doctor` is green.
### 11.10 CI
- `ruff` + `mypy` clean; test matrix on Python 3.11/3.12; SQLite path runs everywhere; Qdrant
  path runs via a service container.
### 11.11 One-line install E2E
- Clean container: default install → memory works end-to-end. Separately: `setup --backend
  qdrant --yes` on a Docker-enabled runner → memory works end-to-end.
---
## 12. README requirements (public-repo readiness)
Must include, in this order: one-sentence pitch; a 20-second demo GIF/asciicast placeholder;
**the two one-line installs (default vs vector) side by side** with a plain-English "use FTS
until you have ~10k+ memories, then switch"; the architecture diagram (two front-doors, one
backend); how the hook + MCP register in Claude Code; the **model-consistency warning** (I7)
called out in its own box, not a footnote; the tuning section (threshold/k + retrieval logs);
a backend comparison table (FTS vs Qdrant vs pgvector: setup cost, scale, ranks-by-meaning);
and a "designed for optimization" section pointing at §8's knobs. Keep the default path
frictionless above the fold; put vector/scale content below.
---
## 13. Config surface (single `config.py`, precedence defaults ← file ← env ← flags)
Ship a documented `~/.claude-memory/config.toml` with every knob from §8 present and defaulted.
Env vars mirror keys (`CLAUDE_MEMORY_RETRIEVAL_THRESHOLD`, etc.). `--yes` and `--backend`
are the only flags needed for one-line setup.
---
## 14. Open decisions left to the implementer (pick sensibly, document the choice)
- BM25→0..1 squash function (suggest a logistic on rank-score; document constants).
- Session-end summarization writer: implement now vs. leave a documented hook point. (Explicit
  `remember` is the MVP; summarization can be Phase 6.)
- pgvector backend: stub with interface + tests skipped, or full. Stub is acceptable for v1.
- Manifest clustering method at scale (tag rollup is fine for v1; embedding-cluster labels are
  a nice-to-have).
---
*End of handover. Build to the invariants; improve everything else.*
