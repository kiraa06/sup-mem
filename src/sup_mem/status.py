"""`sup-mem status` — one glance: is everything actually wired? (Phase 7)

`doctor` checks the backend; `status` checks the INTEGRATION — the part that historically
breaks: hook registrations drifting out of settings.json, the MCP server pointing at a
missing binary, the ledger going quiet. Every failing check carries its fix command.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hook_commands(settings: dict[str, Any], event: str) -> list[str]:
    return [
        str(hook.get("command", ""))
        for entry in settings.get("hooks", {}).get(event, [])
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    ]


def collect_checks(
    config: Config, *, claude_dir: Path | None = None, claude_json: Path | None = None
) -> list[Check]:
    from sup_mem.registration import HOOK_EVENTS, MCP_SERVER_NAME, claude_config_dir

    checks: list[Check] = []
    directory = claude_config_dir(claude_dir)
    settings = _load_json(directory / "settings.json")
    mcp_file = claude_json if claude_json is not None else Path.home() / ".claude.json"

    # 1) hooks registered + their binaries exist
    for event, script in HOOK_EVENTS.items():
        commands = [c for c in _hook_commands(settings, event) if script in c]
        if not commands:
            checks.append(Check(f"hook:{event}", False, "not registered", "run: sup-mem init"))
            continue
        binary = commands[0]
        exists = Path(binary).exists() if "/" in binary else shutil.which(binary) is not None
        checks.append(
            Check(
                f"hook:{event}",
                exists,
                binary if exists else f"registered but missing: {binary}",
                "" if exists else "reinstall: uv tool install --force --reinstall sup-mem",
            )
        )

    # 2) MCP server registered + binary exists
    servers = _load_json(mcp_file).get("mcpServers", {})
    entry = servers.get(MCP_SERVER_NAME, {})
    command = str(entry.get("command", ""))
    if not command:
        checks.append(Check("mcp:sup-mem", False, "not in ~/.claude.json", "run: sup-mem init"))
    else:
        exists = Path(command).exists() if "/" in command else shutil.which(command) is not None
        checks.append(
            Check(
                "mcp:sup-mem",
                exists,
                f"{command} serve" if exists else f"registered but missing: {command}",
                "" if exists else "run: sup-mem init",
            )
        )

    # 3) store reachable
    try:
        from sup_mem.backends import get_backend

        backend = get_backend(config)
        try:
            health = backend.health()
        finally:
            backend.close()
        checks.append(
            Check("store", True, f"{health.get('backend')}: {health.get('count')} memories")
        )
    except Exception as exc:
        checks.append(Check("store", False, str(exc), "run: sup-mem doctor"))

    # 4) outcome loop activity
    if config.ledger_db_path.exists():
        try:
            from sup_mem.ledger import Ledger

            with Ledger(config.ledger_db_path) as ledger:
                stats = ledger.all_stats()
            attributed = sum(s["referenced"] + s["ignored"] + s["contradicted"] for s in stats)
            last = max((s["last_injected"] for s in stats), default="")
            detail = f"{attributed} attributed; last activity {last[:19] or 'n/a'}"
            checks.append(Check("ledger", True, detail))
        except Exception as exc:
            checks.append(Check("ledger", False, str(exc)))
    else:
        checks.append(Check("ledger", True, "empty (fills after a restart + normal use)"))

    # 5) retrieval log size
    log = config.retrieval_log_path
    if log.exists():
        size_kb = log.stat().st_size / 1024
        lines = sum(1 for _ in log.open(encoding="utf-8"))
        checks.append(Check("retrieval-log", True, f"{lines} lines, {size_kb:.0f} KB"))
    else:
        checks.append(Check("retrieval-log", True, "not created yet"))

    # 6) backups freshness
    backups = (
        sorted((d.name for d in config.backups_dir.iterdir() if d.is_dir()), reverse=True)
        if config.backups_dir.is_dir()
        else []
    )
    # Informational: a fresh install has no backups yet; the actionable red is `service`.
    checks.append(
        Check(
            "backups",
            True,
            f"latest {backups[0]}" if backups else "none yet",
            "" if backups else "run: sup-mem maintain (or sup-mem service install)",
        )
    )

    # 7) scheduled service
    try:
        from sup_mem import service

        if service.plist_path().exists():
            is_loaded = service.loaded()
            checks.append(
                Check(
                    "service",
                    is_loaded,
                    "launchd loaded" if is_loaded else "plist present but not loaded",
                    "" if is_loaded else "run: sup-mem service install",
                )
            )
        else:
            checks.append(Check("service", False, "not installed", "run: sup-mem service install"))
    except Exception as exc:
        checks.append(Check("service", False, str(exc)))

    # 8) last maintain run
    stamp = config.maintain_stamp_path
    if stamp.exists():
        checks.append(Check("last-maintain", True, stamp.read_text().strip()[:19]))
    else:
        checks.append(Check("last-maintain", True, "never (runs on schedule or manually)"))

    return checks
