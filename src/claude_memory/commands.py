"""CLI command implementations (HANDOVER §7). Every command is idempotent + re-runnable.

Registration-dependent commands (``init`` / ``setup``) live here too but delegate the
Claude Code settings merge to ``registration.py`` (non-clobbering).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from claude_memory.config import Config


def cmd_doctor(config: Config) -> int:
    """Report backend/service health and enforce embedding-model consistency (I7, §11.9)."""
    from rich.console import Console
    from rich.table import Table

    from claude_memory.backends import get_backend
    from claude_memory.embedding.base import EmbeddingError

    console = Console()
    try:
        backend = get_backend(config)
    except Exception as exc:
        console.print(f"[red]✗[/] could not open backend {config.backend!r}: {exc}")
        return 1

    exit_code = 0
    try:
        try:
            health = backend.health()
        except Exception as exc:
            console.print(f"[red]✗[/] backend unreachable: {exc}")
            return 1

        table = Table(title="claude-memory doctor", show_header=False, title_justify="left")
        table.add_row("backend", str(health.get("backend")))
        table.add_row("data dir", str(config.data_dir))
        table.add_row("memories", str(health.get("count")))
        emb = health.get("embedding")
        table.add_row(
            "embedding",
            "none (lexical)"
            if emb is None
            else f"{emb['provider']} / {emb['model']} (dim {emb['dim']})",
        )
        for key in ("url", "collection", "db_path"):
            if key in health:
                table.add_row(key, str(health[key]))
        console.print(table)

        # I7 — vector backends expose check_consistency(); mismatch exits non-zero (§11.9).
        check = getattr(backend, "check_consistency", None)
        if callable(check):
            try:
                check()
                console.print("[green]✓[/] embedding-model consistency OK (I7)")
            except EmbeddingError as exc:
                console.print(f"[red]✗ embedding-model mismatch (I7):[/] {exc}")
                exit_code = 1
            except Exception as exc:  # unreachable service, etc.
                console.print(f"[yellow]! could not verify embedding consistency:[/] {exc}")
    finally:
        backend.close()

    console.print("[green]healthy[/]" if exit_code == 0 else "[red]problems found[/]")
    return exit_code


def cmd_reindex(config: Config) -> int:
    """Re-embed the store with the current model (vector backends), with a progress bar."""
    from rich.console import Console
    from rich.progress import BarColumn, Progress, TextColumn

    from claude_memory.backends import get_backend

    console = Console()
    backend = get_backend(config)
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task = progress.add_task("Re-embedding", total=1)

            def _cb(done: int, total: int) -> None:
                progress.update(task, completed=done, total=max(total, 1))

            backend.reindex(progress=_cb)
            progress.update(task, completed=progress.tasks[0].total or 1)
        console.print("[green]✓[/] reindex complete")
        return 0
    finally:
        backend.close()


def cmd_serve(config: Config) -> int:
    """Run the long-lived MCP server (blocks)."""
    from claude_memory.mcp.server import serve

    serve(config)
    return 0


def cmd_manifest(config: Config) -> int:
    """Print (and warm the cache for) the scale-aware topic manifest."""
    from claude_memory.backends import get_backend
    from claude_memory.manifest import build_manifest

    backend = get_backend(config)
    try:
        text = build_manifest(backend, config)
    finally:
        backend.close()
    sys.stdout.write((text or "(store is empty — nothing to inject yet)") + "\n")
    return 0


PINNED_FACTS_TEMPLATE = """# Pinned facts (Tier 0 — injected into EVERY turn, verbatim)
#
# Keep this short: a handful of durable, always-relevant facts. '#' lines are notes.
# Replace the examples below with your own.
#
# - I prefer concise answers and code that matches the surrounding style.
# - (add stable facts about you, your stack, or standing preferences here)
"""

_COMPOSE_YAML = """services:
  qdrant:
    image: qdrant/qdrant:v1.12.4
    container_name: claude-memory-qdrant
    restart: unless-stopped
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_storage:/qdrant/storage

volumes:
  qdrant_storage:
"""


def cmd_init(
    config: Config,
    *,
    claude_dir: Path | None = None,
    claude_json: Path | None = None,
    use_cli: bool = True,
) -> int:
    """Default one-liner: create the SQLite FTS store + register with Claude Code (§7)."""
    from rich.console import Console

    from claude_memory.backends import get_backend
    from claude_memory.config import render_default_toml
    from claude_memory.registration import register_into_claude_code

    console = Console()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        config.config_path.write_text(render_default_toml(config), encoding="utf-8")
    if not config.pinned_facts_path.exists():
        config.pinned_facts_path.write_text(PINNED_FACTS_TEMPLATE, encoding="utf-8")
    get_backend(config).close()  # creates the SQLite FTS schema

    report = register_into_claude_code(
        config, claude_dir=claude_dir, claude_json=claude_json, use_cli=use_cli
    )
    console.print(f"[green]✓[/] claude-memory ready (SQLite FTS) — {config.data_dir}")
    console.print(
        f"  hooks      → {report['settings_path']} "
        f"({'updated' if report['hooks_changed'] else 'already registered'})"
    )
    console.print(
        f"  MCP server → {report['mcp_target']} "
        f"({'updated' if report['mcp_changed'] else 'already registered'})"
    )
    console.print(f"  pinned facts: {config.pinned_facts_path}")
    console.print("[yellow]Restart Claude Code[/] to load the hook + MCP server.")
    return 0


def _compose_up(console: Console) -> None:
    """Best-effort: bring up the Qdrant service. Warns and continues on any problem."""
    import shutil
    import subprocess
    import tempfile

    if shutil.which("docker") is None:
        console.print("[yellow]![/] docker not found — start Qdrant yourself, then re-run setup.")
        return
    compose_file = Path.cwd() / "docker-compose.qdrant.yml"
    if not compose_file.exists():
        compose_file = Path(tempfile.gettempdir()) / "claude-memory-docker-compose.qdrant.yml"
        compose_file.write_text(_COMPOSE_YAML, encoding="utf-8")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        console.print("[green]✓[/] Qdrant is up (docker compose)")
    except Exception as exc:
        console.print(
            f"[yellow]![/] could not start Qdrant automatically ({exc}); "
            "start it yourself and re-run setup."
        )


def cmd_setup(
    config: Config,
    backend_name: str,
    *,
    assume_yes: bool = False,
    claude_dir: Path | None = None,
    claude_json: Path | None = None,
    use_cli: bool = True,
) -> int:
    """Set up a backend (§7). Qdrant: docker up → detect embedder → collection → register."""
    from rich.console import Console

    console = Console()
    if backend_name == "sqlite_fts":
        return cmd_init(config, claude_dir=claude_dir, claude_json=claude_json, use_cli=use_cli)
    if backend_name != "qdrant":
        console.print(f"[red]setup for backend {backend_name!r} is not supported[/]")
        return 1

    from claude_memory.backends import get_backend
    from claude_memory.config import load_config, render_default_toml
    from claude_memory.embedding.base import EmbeddingError
    from claude_memory.embedding.detect import detect_embedding_provider
    from claude_memory.registration import register_into_claude_code

    config.data_dir.mkdir(parents=True, exist_ok=True)
    _compose_up(console)
    try:
        selection = detect_embedding_provider(config, assume_yes=assume_yes, log=console.print)
    except EmbeddingError as exc:
        console.print(f"[red]✗[/] {exc}")
        return 1

    merged = load_config(
        overrides={
            "data_dir": str(config.data_dir),
            "backend": "qdrant",
            "embedding": {"provider": selection.provider, "model": selection.model},
        }
    )
    merged.config_path.write_text(render_default_toml(merged), encoding="utf-8")

    backend = get_backend(merged)
    try:
        meta = backend.initialize()  # type: ignore[attr-defined]  # QdrantBackend only
        console.print(
            f"[green]✓[/] collection '{merged.qdrant.collection}' ready; "
            f"embedding recorded: {meta.provider}/{meta.model} (dim {meta.dim})"
        )
    except Exception as exc:
        console.print(f"[red]✗[/] could not initialize the Qdrant collection: {exc}")
        return 1
    finally:
        backend.close()

    report = register_into_claude_code(
        merged, claude_dir=claude_dir, claude_json=claude_json, use_cli=use_cli
    )
    console.print(f"[green]✓[/] claude-memory set up (Qdrant @ {merged.qdrant.url})")
    console.print(
        f"  hooks      → {report['settings_path']} "
        f"({'updated' if report['hooks_changed'] else 'already registered'})"
    )
    console.print("[yellow]Restart Claude Code[/] to load the hook + MCP server.")
    return 0
