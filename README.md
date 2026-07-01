# claude-memory

> A self-hosted, pluggable **global memory layer for Claude** that persists context across
> sessions. The default install is **model-free and Docker-free** with a one-line setup;
> scale up to vector search when you need it. Built for **ultra-fast per-turn retrieval**.

> **Status:** under construction. This README is a stub — the full documentation (§12 of
> [HANDOVER.md](HANDOVER.md)) lands in Phase 5. See the handover for the design contract.

## Install (default — FTS, no Docker, no model)

```bash
curl -LsSf https://raw.githubusercontent.com/kiranjose/claude-memory/main/install.sh | sh
```

## Scale up (opt-in vector search)

```bash
claude-memory setup --backend qdrant --yes
```

## How it works

Two front-doors over one backend: an automatic **hook** injects relevant memory every turn,
and **MCP tools** (`remember` / `recall`) let Claude read and write explicitly. The default
backend is SQLite FTS5 (BM25, zero dependencies); Qdrant is an opt-in vector backend.

See [HANDOVER.md](HANDOVER.md) for the architecture, invariants, and full spec.

## License

MIT — see [LICENSE](LICENSE).
