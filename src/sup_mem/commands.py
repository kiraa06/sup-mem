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

    from sup_mem.config import Config


def cmd_doctor(config: Config) -> int:
    """Report backend/service health and enforce embedding-model consistency (I7, §11.9)."""
    from rich.console import Console
    from rich.table import Table

    from sup_mem.backends import get_backend
    from sup_mem.embedding.base import EmbeddingError

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

        table = Table(title="sup-mem doctor", show_header=False, title_justify="left")
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

    from sup_mem.backends import get_backend

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
    from sup_mem.mcp.server import serve

    serve(config)
    return 0


def cmd_manifest(config: Config) -> int:
    """Print (and warm the cache for) the scale-aware topic manifest."""
    from sup_mem.backends import get_backend
    from sup_mem.manifest import build_manifest

    backend = get_backend(config)
    try:
        text = build_manifest(backend, config)
    finally:
        backend.close()
    sys.stdout.write((text or "(store is empty — nothing to inject yet)") + "\n")
    return 0


def cmd_migrate_native(
    config: Config, *, projects_dir: Path | None = None, dry_run: bool = False
) -> int:
    """Copy Claude Code's built-in file memories into the sup-mem store (copy-only)."""
    from rich.console import Console

    from sup_mem.backends import get_backend
    from sup_mem.migrate import migrate_native
    from sup_mem.registration import claude_config_dir

    console = Console()
    source_dir = projects_dir if projects_dir is not None else claude_config_dir() / "projects"
    if not source_dir.is_dir():
        console.print(f"[yellow]![/] no native memory found ({source_dir} does not exist)")
        return 0

    backend = get_backend(config)
    try:
        report = migrate_native(backend, source_dir, dry_run=dry_run)
    finally:
        backend.close()

    verb = "would migrate" if dry_run else "migrated"
    for source, kind, chars in report["migrated"]:
        console.print(f"  [{kind:9}] {source}  ({chars} chars)")
    for source in report["skipped_empty"]:
        console.print(f"  [dim]skipped (empty)[/] {source}")
    console.print(
        f"[green]✓[/] {verb} {len(report['migrated'])} memories from {source_dir}"
        + (
            f" — {report['new']} new, "
            f"{len(report['migrated']) - report['new']} already present; "
            f"store now holds {report['total']}"
            if not dry_run
            else " (dry run — nothing stored)"
        )
    )
    if not dry_run:
        console.print("  source files untouched; re-running is safe (dedupes on content+source)")
    return 0


def cmd_tune(config: Config, *, apply: bool = False) -> int:
    """Counterfactual threshold replay against recorded outcomes (PHASE6, honest per L4)."""
    from rich.console import Console
    from rich.table import Table

    from sup_mem.ledger import Ledger

    console = Console()
    with Ledger(config.ledger_db_path) as ledger:
        turns = ledger.candidate_turns()
    attributed = sum(1 for t in turns for c in t if c["injected"] and c["outcome"])
    if not turns or attributed == 0:
        console.print(
            "[yellow]Not enough outcome data yet.[/] Use Claude Code normally for a while — "
            "the Stop hook records whether injected memories get referenced — then re-run."
        )
        return 0

    k = config.retrieval.k
    current = config.retrieval.threshold
    grid = sorted({round(0.05 * i, 2) for i in range(1, 20)} | {round(current, 2)})

    rows: list[dict[str, float]] = []
    for theta in grid:
        kept_ref = lost_ref = kept_ign = cut_ign = unknown_added = 0
        tokens_total = 0
        for turn in turns:
            would = [c for c in turn if c["score"] >= theta][:k]
            would_ids = {(c["memory_id"]) for c in would}
            tokens_total += sum(c["tokens"] for c in would)
            for cand in turn:
                inj, outcome = bool(cand["injected"]), str(cand["outcome"])
                in_would = cand["memory_id"] in would_ids
                if inj and outcome in ("referenced", "contradicted"):
                    kept_ref += in_would and outcome == "referenced"
                    lost_ref += (not in_would) and outcome == "referenced"
                elif inj and outcome == "ignored":
                    kept_ign += in_would
                    cut_ign += not in_would
                elif not inj and in_would:
                    unknown_added += 1  # below the live threshold then → outcome unknown (L4)
        rows.append(
            {
                "theta": theta,
                "kept_ref": kept_ref,
                "lost_ref": lost_ref,
                "kept_ign": kept_ign,
                "cut_ign": cut_ign,
                "unknown": unknown_added,
                "tok_per_turn": tokens_total / max(len(turns), 1),
            }
        )

    # Recommend the highest threshold that loses zero referenced injections.
    keepers = [r for r in rows if r["lost_ref"] == 0]
    recommended = max(keepers, key=lambda r: r["theta"])["theta"] if keepers else current

    table = Table(title=f"sup-mem tune — {len(turns)} logged turns, {attributed} attributed")
    for col in ("θ", "ref kept", "ref lost", "ign kept", "ign cut", "unknown+", "tok/turn"):
        table.add_column(col, justify="right")
    for r in rows:
        mark = " ←now" if r["theta"] == round(current, 2) else ""
        mark += " ★rec" if r["theta"] == recommended else ""
        table.add_row(
            f"{r['theta']:.2f}{mark}",
            str(int(r["kept_ref"])),
            str(int(r["lost_ref"])),
            str(int(r["kept_ign"])),
            str(int(r["cut_ign"])),
            str(int(r["unknown"])),
            f"{r['tok_per_turn']:.0f}",
        )
    console.print(table)
    console.print(
        f"Recommended threshold: [bold]{recommended:.2f}[/] "
        "(highest that keeps every referenced injection). "
        "'unknown+' candidates were never injected, so their outcomes are unknown — "
        "lowering the threshold is a guess; raising it is evidence-based."
    )

    if apply and recommended != current:
        from sup_mem.config import load_config, render_default_toml

        updated = load_config(
            overrides={"data_dir": str(config.data_dir), "retrieval": {"threshold": recommended}}
        )
        updated.config_path.write_text(render_default_toml(updated), encoding="utf-8")
        console.print(
            f"[green]✓[/] wrote retrieval.threshold = {recommended} → {updated.config_path}"
        )
    elif apply:
        console.print("Current threshold already matches the recommendation — nothing written.")
    return 0


def cmd_roi(config: Config) -> int:
    """Token P&L per memory: what each memory costs in context vs. what it contributes."""
    from rich.console import Console
    from rich.table import Table

    from sup_mem.backends import get_backend
    from sup_mem.ledger import Ledger

    console = Console()
    with Ledger(config.ledger_db_path) as ledger:
        stats = ledger.all_stats()
    if not stats:
        console.print("[yellow]No outcome data yet[/] — the ledger fills as you use Claude Code.")
        return 0

    backend = get_backend(config)
    try:
        texts = backend.fetch([s["memory_id"] for s in stats])
    finally:
        backend.close()

    led = config.ledger
    table = Table(title="sup-mem roi — token P&L per memory (highest spend first)")
    for col, justify in (
        ("memory", "left"),
        ("inj", "right"),
        ("tokens", "right"),
        ("ref", "right"),
        ("ign", "right"),
        ("contra", "right"),
        ("verdict", "left"),
    ):
        table.add_column(col, justify=justify)  # type: ignore[arg-type]

    totals = {"injected": 0, "tokens": 0, "referenced": 0, "ignored": 0, "contradicted": 0}
    for s in stats:
        for key in totals:
            totals[key] += int(s[key])
        if (
            s["contradicted"] >= led.quarantine_contradictions
            and s["contradicted"] > s["referenced"]
        ):
            verdict = "[red]quarantined[/]"
        elif s["referenced"] > 0:
            verdict = "[green]valuable[/]"
        elif s["injected"] >= 3:
            verdict = "[yellow]wasteful[/]"
        else:
            verdict = "watching"
        snippet = texts.get(s["memory_id"], s["memory_id"])[:57].replace("\n", " ")
        table.add_row(
            snippet,
            str(s["injected"]),
            str(s["tokens"]),
            str(s["referenced"]),
            str(s["ignored"]),
            str(s["contradicted"]),
            verdict,
        )
    console.print(table)
    ref_rate = totals["referenced"] / max(totals["injected"], 1)
    console.print(
        f"Totals: {totals['injected']} injections, ~{totals['tokens']} tokens, "
        f"{totals['referenced']} referenced ({ref_rate:.0%}), "
        f"{totals['ignored']} ignored, {totals['contradicted']} contradicted."
    )
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
    container_name: sup-mem-qdrant
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

    from sup_mem.backends import get_backend
    from sup_mem.config import render_default_toml
    from sup_mem.registration import register_into_claude_code

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
    console.print(f"[green]✓[/] sup-mem ready (SQLite FTS) — {config.data_dir}")
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
        compose_file = Path(tempfile.gettempdir()) / "sup-mem-docker-compose.qdrant.yml"
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

    from sup_mem.backends import get_backend
    from sup_mem.config import load_config, render_default_toml
    from sup_mem.embedding.base import EmbeddingError
    from sup_mem.embedding.detect import detect_embedding_provider
    from sup_mem.registration import register_into_claude_code

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
    console.print(f"[green]✓[/] sup-mem set up (Qdrant @ {merged.qdrant.url})")
    console.print(
        f"  hooks      → {report['settings_path']} "
        f"({'updated' if report['hooks_changed'] else 'already registered'})"
    )
    console.print("[yellow]Restart Claude Code[/] to load the hook + MCP server.")
    return 0
