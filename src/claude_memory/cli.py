"""Command-line interface for claude-memory (HANDOVER §7).

Phase 0 ships the full argument surface plus stubs; the command bodies are implemented in
Phase 4 (init / setup / doctor / reindex / serve / manifest). Keeping the parser complete now
lets ``claude-memory --help`` work (the Phase 0 acceptance gate) and lets tests introspect it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from claude_memory import __version__

_PENDING = "not implemented yet (Phase 4)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-memory",
        description="Self-hosted, pluggable global memory layer for Claude.",
    )
    parser.add_argument("--version", action="version", version=f"claude-memory {__version__}")
    parser.add_argument(
        "--data-dir",
        default=None,
        metavar="PATH",
        help="Override the data directory (default: ~/.claude-memory).",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser(
        "init",
        help="Create the default SQLite FTS store and register the hook + MCP server.",
    )

    p_setup = sub.add_parser("setup", help="Set up a backend (e.g. --backend qdrant).")
    p_setup.add_argument(
        "--backend",
        default="qdrant",
        choices=["sqlite_fts", "qdrant", "pgvector"],
        help="Backend to configure (default: qdrant).",
    )
    p_setup.add_argument(
        "-y", "--yes", action="store_true", help="Non-interactive; auto-pick the embedder."
    )

    sub.add_parser(
        "doctor", help="Report backend/service health and enforce embedding-model consistency."
    )
    sub.add_parser("reindex", help="Re-embed the store with the current model (vector backends).")
    sub.add_parser("serve", help="Run the long-lived MCP server.")
    sub.add_parser("manifest", help="Print/refresh the scale-aware topic manifest.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    print(f"claude-memory {args.command}: {_PENDING}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
