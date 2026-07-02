"""Command-line interface for sup-mem (HANDOVER §7).

Phase 0 ships the full argument surface plus stubs; the command bodies are implemented in
Phase 4 (init / setup / doctor / reindex / serve / manifest). Keeping the parser complete now
lets ``sup-mem --help`` work (the Phase 0 acceptance gate) and lets tests introspect it.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from sup_mem import __version__
from sup_mem.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sup-mem",
        description="Self-hosted, pluggable global memory layer for Claude.",
    )
    parser.add_argument("--version", action="version", version=f"sup-mem {__version__}")
    parser.add_argument(
        "--data-dir",
        default=None,
        metavar="PATH",
        help="Override the data directory (default: ~/.sup-mem).",
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

    p_migrate = sub.add_parser(
        "migrate-native",
        help="Copy Claude Code's built-in file memories (~/.claude/projects/*/memory) "
        "into the store. Copy-only and idempotent.",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true", help="List what would be migrated without storing."
    )

    p_tune = sub.add_parser(
        "tune",
        help="Counterfactually replay logged retrievals against recorded outcomes and "
        "recommend a threshold (the outcome loop).",
    )
    p_tune.add_argument(
        "--apply", action="store_true", help="Write the recommended threshold into config.toml."
    )

    sub.add_parser(
        "roi",
        help="Token P&L per memory: injections, tokens consumed, referenced/ignored/"
        "contradicted, verdicts.",
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

    overrides: dict[str, object] = {}
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    config = load_config(overrides=overrides)

    from sup_mem import commands

    if args.command == "init":
        return commands.cmd_init(config)
    if args.command == "setup":
        return commands.cmd_setup(config, args.backend, assume_yes=args.yes)
    if args.command == "migrate-native":
        return commands.cmd_migrate_native(config, dry_run=args.dry_run)
    if args.command == "tune":
        return commands.cmd_tune(config, apply=args.apply)
    if args.command == "roi":
        return commands.cmd_roi(config)
    if args.command == "doctor":
        return commands.cmd_doctor(config)
    if args.command == "reindex":
        return commands.cmd_reindex(config)
    if args.command == "serve":
        return commands.cmd_serve(config)
    if args.command == "manifest":
        return commands.cmd_manifest(config)
    parser.print_help()  # unknown command (shouldn't happen via argparse)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
