"""Background scheduling for `sup-mem maintain` — a macOS LaunchAgent (Phase 7).

`sup-mem service install` writes ``~/Library/LaunchAgents/com.sup-mem.maintain.plist`` and
bootstraps it with launchctl, so maintenance runs daily with no human in the loop. Install
and uninstall are idempotent. Non-macOS platforms get an equivalent cron line instead.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config

LABEL = "com.sup-mem.maintain"

# Injectable for tests; matches subprocess.run's shape.
Runner = Any


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _sup_mem_binary() -> str:
    # Prefer the stable uv-tool install over a project venv copy.
    home_bin = Path.home() / ".local" / "bin" / "sup-mem"
    if home_bin.exists():
        return str(home_bin)
    return shutil.which("sup-mem") or "sup-mem"


def render_plist(config: Config) -> bytes:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [_sup_mem_binary(), "maintain"],
        "StartCalendarInterval": {
            "Hour": config.maintenance.hour,
            "Minute": config.maintenance.minute,
        },
        "StandardOutPath": str(config.logs_dir / "maintain.log"),
        "StandardErrorPath": str(config.logs_dir / "maintain.log"),
        "RunAtLoad": False,
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)


def _is_macos() -> bool:
    # A function boundary on purpose: mypy narrows literal `sys.platform` comparisons by the
    # analysis platform, which makes the other OS's branch "unreachable" under
    # warn_unreachable (breaks lint on Linux CI). Runtime behavior is identical.
    return sys.platform == "darwin"


def _launchctl(args: list[str], runner: Runner = subprocess.run) -> tuple[int, str]:
    proc = runner(["launchctl", *args], capture_output=True, text=True, timeout=30)
    out = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
    return int(proc.returncode), out


def install(config: Config, runner: Runner = subprocess.run) -> tuple[bool, str]:
    if not _is_macos():
        line = f"{config.maintenance.minute} {config.maintenance.hour} * * * sup-mem maintain"
        return False, f"launchd is macOS-only; add this crontab line instead:\n  {line}"
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_plist(config))

    domain = f"gui/{os.getuid()}"
    _launchctl(["bootout", f"{domain}/{LABEL}"], runner)  # clear any stale load; ok to fail
    code, out = _launchctl(["bootstrap", domain, str(path)], runner)
    if code != 0:
        return False, f"wrote {path} but bootstrap failed: {out or code}"
    schedule = f"{config.maintenance.hour:02d}:{config.maintenance.minute:02d}"
    return True, f"{LABEL} loaded — daily at {schedule} → {config.logs_dir / 'maintain.log'}"


def uninstall(runner: Runner = subprocess.run) -> tuple[bool, str]:
    if not _is_macos():
        return False, "launchd is macOS-only; remove your crontab line instead."
    _launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"], runner)  # ok to fail if not loaded
    path = plist_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return True, f"{LABEL} unloaded{' and plist removed' if existed else ' (no plist found)'}"


def loaded(runner: Runner = subprocess.run) -> bool:
    if not _is_macos():
        return False
    code, _ = _launchctl(["print", f"gui/{os.getuid()}/{LABEL}"], runner)
    return code == 0
