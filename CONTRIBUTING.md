# Contributing

## Setup

```bash
git clone https://github.com/kiraa06/sup-mem && cd sup-mem
uv sync                    # base (SQLite) dev env — this is all most changes need
uv sync --extra qdrant     # vector path (tests skip gracefully without a live Qdrant)
```

## Branching & releases

`main` is protected — **no direct pushes**; everything lands via PR with green CI.

- **Features** → `feat/<slug>` → PR into `main`.
- **Bugfixes** → `fix/<slug>` → PR into `main` (kept separate from features).
- Everything else → `docs/<slug>` / `chore/<slug>`. Keep PRs focused; the prefix says the kind.
- **Releases**: merge to `main`, then tag `vX.Y.Z` on `main` — the release workflow builds,
  cuts the GitHub Release, and publishes to PyPI. Bump the Homebrew formula's url+sha256 after.

Required checks before a PR can merge: `Lint + type` and `Tests (SQLite path …)` (3.11 + 3.12).

## The gate (every PR must pass it — CI enforces the same)

```bash
uv run ruff format . && uv run ruff check .
uv run mypy                       # strict; must also pass --platform linux
uv run pytest -q -m "not qdrant"  # fast suite
# vector path, if you touched backends:
docker compose -f docker-compose.qdrant.yml up -d
QDRANT_URL=http://localhost:6333 uv run pytest -q -m qdrant
```

## The contracts

This codebase is built against written invariants — read them before changing behavior:

- `HANDOVER.md` §2 — the original ten (I1–I10): hot-path hook never loads a model, regex is
  a skip-gate never a relevance-gate, fail-open everywhere, everything tunable in config…
- `docs/PHASE6-LOOP.md` (L1–L5), `docs/PHASE8-TEMPORAL.md` (T1–T6),
  `docs/PHASE9-ARCHIVAL.md` (A1–A6)

If a change would violate an invariant, the invariant wins — open an issue to argue for
amending it instead (Phase 9's deletion amendment shows the pattern: explicit, documented,
chain-audited).

Two non-negotiables that bite newcomers:

1. **The UserPromptSubmit hook's Tier-1 skip path must import nothing heavy** — there's a
   subprocess test asserting `sys.modules` stays clean; it is build-breaking on purpose.
2. **Tests must be hermetic**: never touch the developer's real `~/.claude*` or `~/.sup-mem`
   (use the `config`/`make_config` fixtures; registration tests pass `use_cli=False`).

## Style

Match what's here: ruff-formatted, 100-col, mypy-strict, comments explain *constraints*,
not narration. Conventional commits (`feat:`, `fix:`, `docs:`…).
