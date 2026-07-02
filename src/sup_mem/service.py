"""Background scheduling for `sup-mem maintain` (Phase 7).

Platform-native, one command each way:
  macOS  → launchd LaunchAgent  (~/Library/LaunchAgents/com.sup-mem.maintain.plist)
  Linux  → systemd user timer   (~/.config/systemd/user/sup-mem-maintain.{service,timer})
  other / no systemd → prints the equivalent crontab line instead.

Install and uninstall are idempotent; launchctl/systemctl calls go through an injectable
``runner`` so tests never touch the real service managers.
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

LABEL = "com.sup-mem.maintain"  # launchd label
SYSTEMD_UNIT = "sup-mem-maintain"  # systemd unit base name

# Injectable for tests; matches subprocess.run's shape.
Runner = Any


def _is_macos() -> bool:
    # A function boundary on purpose: mypy narrows literal `sys.platform` comparisons by the
    # analysis platform, which makes the other OS's branch "unreachable" under
    # warn_unreachable (breaks lint on cross-platform CI). Runtime behavior is identical.
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _has_systemctl() -> bool:
    return shutil.which("systemctl") is not None


def scheduler_kind() -> str:
    """Which scheduler this host can use: ``launchd`` | ``systemd`` | ``none``."""
    if _is_macos():
        return "launchd"
    if _is_linux() and _has_systemctl():
        return "systemd"
    return "none"


def _sup_mem_binary() -> str:
    # Prefer the stable uv-tool install over a project venv copy.
    home_bin = Path.home() / ".local" / "bin" / "sup-mem"
    if home_bin.exists():
        return str(home_bin)
    return shutil.which("sup-mem") or "sup-mem"


def _cron_line(config: Config) -> str:
    return f"{config.maintenance.minute} {config.maintenance.hour} * * * sup-mem maintain"


def _run(cmd: list[str], runner: Runner = subprocess.run) -> tuple[int, str]:
    proc = runner(cmd, capture_output=True, text=True, timeout=30)
    out = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
    return int(proc.returncode), out


# --------------------------------------------------------------------------------------------
# launchd (macOS)
# --------------------------------------------------------------------------------------------
def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


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


def _install_launchd(config: Config, runner: Runner) -> tuple[bool, str]:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_plist(config))
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{LABEL}"], runner)  # clear stale load; ok to fail
    code, out = _run(["launchctl", "bootstrap", domain, str(path)], runner)
    if code != 0:
        return False, f"wrote {path} but bootstrap failed: {out or code}"
    schedule = f"{config.maintenance.hour:02d}:{config.maintenance.minute:02d}"
    return True, f"{LABEL} loaded — daily at {schedule} → {config.logs_dir / 'maintain.log'}"


# --------------------------------------------------------------------------------------------
# systemd user timer (Linux)
# --------------------------------------------------------------------------------------------
def systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def render_systemd_service(config: Config) -> str:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log = config.logs_dir / "maintain.log"
    return (
        "[Unit]\n"
        "Description=sup-mem housekeeping (rotate, backup, sweep, tune, health)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_sup_mem_binary()} maintain\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
    )


def render_systemd_timer(config: Config) -> str:
    return (
        "[Unit]\n"
        "Description=Daily sup-mem maintenance\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {config.maintenance.hour:02d}:{config.maintenance.minute:02d}:00\n"
        "Persistent=true\n\n"  # a missed run (machine off/asleep) fires on the next boot
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _install_systemd(config: Config, runner: Runner) -> tuple[bool, str]:
    unit_dir = systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{SYSTEMD_UNIT}.service").write_text(
        render_systemd_service(config), encoding="utf-8"
    )
    (unit_dir / f"{SYSTEMD_UNIT}.timer").write_text(render_systemd_timer(config), encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"], runner)
    code, out = _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"], runner)
    if code != 0:
        return False, f"wrote units in {unit_dir} but enable failed: {out or code}"
    schedule = f"{config.maintenance.hour:02d}:{config.maintenance.minute:02d}"
    return True, (
        f"{SYSTEMD_UNIT}.timer enabled — daily at {schedule} → {config.logs_dir / 'maintain.log'}"
    )


# --------------------------------------------------------------------------------------------
# public surface (platform dispatch)
# --------------------------------------------------------------------------------------------
def install(config: Config, runner: Runner = subprocess.run) -> tuple[bool, str]:
    kind = scheduler_kind()
    if kind == "launchd":
        return _install_launchd(config, runner)
    if kind == "systemd":
        return _install_systemd(config, runner)
    return False, (
        "no supported scheduler here (launchd/systemd); add this crontab line instead:\n"
        f"  {_cron_line(config)}"
    )


def uninstall(runner: Runner = subprocess.run) -> tuple[bool, str]:
    kind = scheduler_kind()
    if kind == "launchd":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], runner)  # ok to fail
        path = plist_path()
        existed = path.exists()
        if existed:
            path.unlink()
        return True, f"{LABEL} unloaded{' and plist removed' if existed else ' (no plist found)'}"
    if kind == "systemd":
        _run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"], runner)
        removed = 0
        for name in (f"{SYSTEMD_UNIT}.timer", f"{SYSTEMD_UNIT}.service"):
            unit = systemd_dir() / name
            if unit.exists():
                unit.unlink()
                removed += 1
        _run(["systemctl", "--user", "daemon-reload"], runner)
        return True, f"{SYSTEMD_UNIT}.timer disabled ({removed} unit files removed)"
    return False, "no supported scheduler here; remove your crontab line instead."


def installed() -> bool:
    kind = scheduler_kind()
    if kind == "launchd":
        return plist_path().exists()
    if kind == "systemd":
        return (systemd_dir() / f"{SYSTEMD_UNIT}.timer").exists()
    return False


def loaded(runner: Runner = subprocess.run) -> bool:
    kind = scheduler_kind()
    if kind == "launchd":
        code, _ = _run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], runner)
        return code == 0
    if kind == "systemd":
        code, _ = _run(
            ["systemctl", "--user", "is-active", "--quiet", f"{SYSTEMD_UNIT}.timer"], runner
        )
        return code == 0
    return False
