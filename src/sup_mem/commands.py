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

    from sup_mem.ledger import Ledger, attributed_count, recommend_threshold, replay_thresholds

    console = Console()
    with Ledger(config.ledger_db_path) as ledger:
        turns = ledger.candidate_turns()
    attributed = attributed_count(turns)
    if not turns or attributed == 0:
        console.print(
            "[yellow]Not enough outcome data yet.[/] Use Claude Code normally for a while — "
            "the Stop hook records whether injected memories get referenced — then re-run."
        )
        return 0

    current = config.retrieval.threshold
    rows = replay_thresholds(turns, config.retrieval.k, current)
    recommended = recommend_threshold(rows, current)

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


def _parse_as_of(raw: str) -> str:
    """Normalize --as-of input to a comparable ISO instant.

    A bare date means "end of that day, UTC" — `--as-of 2026-06-01` asks what we believed
    ON June 1, so the whole day counts (PHASE8 T2).
    """
    from datetime import UTC, datetime, time

    value = raw.strip()
    try:
        if len(value) == 10:  # YYYY-MM-DD
            day = datetime.strptime(value, "%Y-%m-%d").date()
            return datetime.combine(day, time.max, tzinfo=UTC).isoformat()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()
    except ValueError as exc:
        raise SystemExit(f"invalid --as-of {raw!r}: use YYYY-MM-DD or an ISO timestamp") from exc


def cmd_recall(
    config: Config,
    query: str,
    *,
    k: int | None = None,
    as_of: str | None = None,
    diff_now: bool = False,
) -> int:
    """Search the store from the CLI; with --as-of, ask what we believed at that instant."""
    from rich.console import Console

    from sup_mem.backends import get_backend

    console = Console()
    instant = _parse_as_of(as_of) if as_of else None
    limit = k if (k and k > 0) else config.retrieval.k

    backend = get_backend(config)
    try:
        try:
            hits = backend.search(query, k=limit, threshold=0.0, as_of=instant)
        except ValueError as exc:  # e.g. qdrant + --as-of (T6)
            console.print(f"[red]✗[/] {exc}")
            return 1
        current_versions = getattr(backend, "current_versions", None)  # sqlite backend only
        currents: dict[str, dict[str, str]] = (
            current_versions([str(h.metadata.get("_lineage", "")) for h in hits])
            if diff_now and hits and callable(current_versions)
            else {}
        )
    finally:
        backend.close()

    if not hits:
        console.print(
            f"no memories matched{f' as of {instant[:19]}' if instant else ''} — "
            "try a broader query"
        )
        return 0

    header = f"as of {instant[:19]} — what the store believed then" if instant else "live memories"
    console.print(f"[bold]{header}[/] ({len(hits)} hit{'s' if len(hits) != 1 else ''})\n")
    for i, hit in enumerate(hits, 1):
        recorded = str(hit.metadata.get("_recorded_at", ""))[:19]
        superseded = hit.metadata.get("_superseded_at")
        stamp = f"recorded {recorded}"
        if superseded:
            stamp += f", [yellow]superseded {str(superseded)[:19]}[/]"
        console.print(f"[bold]{i}.[/] ({hit.score:.2f}, {stamp})")
        console.print(f"   {hit.text}\n")
        if diff_now:
            lineage = str(hit.metadata.get("_lineage", ""))
            current = currents.get(lineage)
            if current is None:
                console.print("   [red]now: retired — no live version of this fact line[/]\n")
            elif current["id"] == hit.id:
                console.print("   [green]now: unchanged — still the live belief[/]\n")
            else:
                console.print(
                    f"   [yellow]now: changed[/] (recorded {current['recorded_at'][:19]}):"
                )
                console.print(f"   {current['text']}\n")
    return 0


def cmd_verify(config: Config, *, quiet: bool = False) -> int:
    """Verify the provenance chain + row hashes (PHASE8 T5). Non-zero on any break."""
    from rich.console import Console

    from sup_mem.backends import get_backend

    console = Console()
    backend = get_backend(config)
    try:
        check = getattr(backend, "verify_provenance", None)
        if not callable(check):
            console.print("[yellow]![/] provenance is not supported on this backend")
            return 0
        report = check()
    finally:
        backend.close()

    if report["ok"]:
        if not quiet:
            console.print(
                f"[green]✓[/] provenance intact — {report['events']} chain events verified"
                + (f" ({report['reason']})" if report["reason"] else "")
            )
        return 0
    console.print(f"[red]✗ provenance verification FAILED:[/] {report['reason']}")
    console.print(
        "  The store changed outside sup-mem's write path. Compare against a backup in "
        f"{config.backups_dir} to recover."
    )
    return 1


def cmd_archive(config: Config, *, dry_run: bool = False, list_mode: bool = False) -> int:
    """Run the three archival regimes (PHASE9), or list the cold tier with --list."""
    from rich.console import Console
    from rich.table import Table

    from sup_mem.archival import run_archival

    console = Console()
    if list_mode:
        from sup_mem.backends import get_backend

        backend = get_backend(config)
        try:
            lister = getattr(backend, "archive_list", None)
            entries = lister() if callable(lister) else []
        finally:
            backend.close()
        if not entries:
            console.print("archive tier is empty")
            return 0
        table = Table(title=f"sup-mem archive — {len(entries)} cold versions")
        for col in ("id", "topic", "archived_at", "bytes"):
            table.add_column(col)
        for e in entries:
            table.add_row(e["id"], e["topic"][:40], e["archived_at"][:19], str(e["bytes"]))
        console.print(table)
        console.print("restore any of these with: sup-mem restore <id>")
        return 0

    report = run_archival(config, dry_run=dry_run)
    if not report["supported"]:
        console.print(f"[yellow]![/] {report['note']}")
        return 0

    verb = "would move" if dry_run else "moved"
    console.print(
        f"steady tier (superseded/quarantined): {verb} {len(report['steady'])}"
        + (f" — {', '.join(report['steady'][:5])}" if report["steady"] else "")
    )
    console.print(f"pressure tier (decay, most-useless first): {verb} {len(report['pressure'])}")
    if report["purged"]:
        console.print(
            f"[red]purged FOREVER (archive over cap, FIFO): {len(report['purged'])}[/] — "
            + ", ".join(f"{topic or mid}" for mid, topic in report["purged"][:5])
        )
    sizes = report.get("sizes_after") or report.get("sizes_before", {})
    console.print(
        f"tiers: main {sizes.get('main', 0) / 1048576:.2f} MB / "
        f"{config.archival.main_max_mb} MB cap · archive "
        f"{sizes.get('archive', 0) / 1048576:.2f} MB / {config.archival.archive_max_mb} MB cap"
    )
    if report["note"]:
        console.print(f"[yellow]{report['note'].strip()}[/]")
    return 0


def cmd_restore(config: Config, memory_ids: list[str]) -> int:
    """Move versions back from the cold tier to the hot store."""
    from rich.console import Console

    from sup_mem.backends import get_backend

    console = Console()
    backend = get_backend(config)
    try:
        restorer = getattr(backend, "restore_versions", None)
        if not callable(restorer):
            console.print("[yellow]![/] restore requires the sqlite_fts backend")
            return 1
        restored = restorer(memory_ids)
    finally:
        backend.close()
    missing = len(memory_ids) - restored
    console.print(
        f"[green]✓[/] restored {restored} version(s) to the hot store"
        + (f"; {missing} id(s) not found in the archive" if missing else "")
    )
    return 0 if restored or not memory_ids else 1


def cmd_maintain(config: Config) -> int:
    """Run all housekeeping steps (Phase 7); exit non-zero if any step failed."""
    from rich.console import Console
    from rich.table import Table

    from sup_mem.maintenance import run_maintenance

    console = Console()
    results = run_maintenance(config)

    table = Table(title="sup-mem maintain")
    table.add_column("step")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    icon = {"ok": "[green]ok[/]", "skipped": "[dim]skipped[/]", "failed": "[red]FAILED[/]"}
    for r in results:
        table.add_row(r.name, icon.get(r.status, r.status), r.detail)
    console.print(table)

    failed = [r for r in results if r.status == "failed"]
    if failed:
        console.print(f"[red]{len(failed)} step(s) failed[/]")
        return 1
    return 0


def cmd_status(
    config: Config, *, claude_dir: Path | None = None, claude_json: Path | None = None
) -> int:
    """One-glance wiring check (Phase 7); exit non-zero if anything needs attention."""
    from rich.console import Console
    from rich.table import Table

    from sup_mem import __version__
    from sup_mem.status import collect_checks

    console = Console()
    checks = collect_checks(config, claude_dir=claude_dir, claude_json=claude_json)

    table = Table(title=f"sup-mem status — v{__version__} · {config.data_dir}")
    table.add_column("check")
    table.add_column("", justify="center")
    table.add_column("detail", overflow="fold")
    table.add_column("fix", overflow="fold")
    for c in checks:
        table.add_row(c.name, "[green]✓[/]" if c.ok else "[red]✗[/]", c.detail, c.fix)
    console.print(table)

    bad = [c for c in checks if not c.ok]
    if bad:
        console.print(f"[red]{len(bad)} check(s) need attention[/]")
        return 1
    console.print("[green]all wired[/]")
    return 0


def cmd_service(config: Config, action: str) -> int:
    """Manage the launchd background service for `maintain` (Phase 7)."""
    from rich.console import Console

    from sup_mem import service

    console = Console()
    if action == "install":
        ok, message = service.install(config)
    elif action == "uninstall":
        ok, message = service.uninstall()
    else:  # status
        is_loaded = service.loaded()
        ok = True
        message = (
            f"{service.LABEL}: {'loaded' if is_loaded else 'not loaded'} "
            f"(plist {'present' if service.plist_path().exists() else 'absent'})"
        )
    console.print(("[green]✓[/] " if ok else "[red]✗[/] ") + message)
    return 0 if ok else 1


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
    clients: list[str] | None = None,
    claude_dir: Path | None = None,
    claude_json: Path | None = None,
    codex_home: Path | None = None,
    gemini_home: Path | None = None,
    use_cli: bool = True,
) -> int:
    """Default one-liner: create the SQLite FTS store + wire the hooks/MCP into the host(s).

    ``clients`` selects which hosts to wire; ``None`` auto-detects installed ones (falling back
    to Claude Code). Each host's registration is non-clobbering with a timestamped backup (§7).
    """
    from rich.console import Console

    from sup_mem.backends import get_backend
    from sup_mem.clients import CLIENTS, detect_installed, get_client
    from sup_mem.config import render_default_toml

    console = Console()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        config.config_path.write_text(render_default_toml(config), encoding="utf-8")
    if not config.pinned_facts_path.exists():
        config.pinned_facts_path.write_text(PINNED_FACTS_TEMPLATE, encoding="utf-8")
    get_backend(config).close()  # creates the SQLite FTS schema

    requested = clients if clients is not None else (detect_installed() or ["claude"])
    seen: set[str] = set()
    selected: list[str] = []
    for name in requested:  # de-dup, drop unknowns, preserve order
        if name in CLIENTS and name not in seen:
            seen.add(name)
            selected.append(name)

    overrides: dict[str, dict[str, object]] = {
        "claude": {"claude_dir": claude_dir, "claude_json": claude_json, "use_cli": use_cli},
        "codex": {"codex_home": codex_home},
        "gemini": {"gemini_home": gemini_home},
    }

    console.print(f"[green]✓[/] sup-mem ready (SQLite FTS) — {config.data_dir}")
    console.print(f"  pinned facts: {config.pinned_facts_path}")
    for name in selected:
        report = get_client(name).register(config, **overrides.get(name, {}))
        console.print(f"[bold]{name}[/]")
        console.print(
            f"  hooks      → {report['settings_path']} "
            f"({'updated' if report['hooks_changed'] else 'already registered'})"
        )
        console.print(
            f"  MCP server → {report['mcp_target']} "
            f"({'updated' if report['mcp_changed'] else 'already registered'})"
        )
    restart = ", ".join(selected)
    console.print(f"[yellow]Restart {restart}[/] to load the hook + MCP server.")
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
