"""Local hardening: owner-only data files, and `setup qdrant` running only its own compose file.

All hermetic — data dirs, temp dirs and the CWD are tmp paths; docker is faked.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from sup_mem import commands
from sup_mem.backends import get_backend
from sup_mem.config import Config, load_config
from sup_mem.hook import user_prompt_submit
from sup_mem.ledger import Ledger


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


# --- data dir 0700, data files 0600 (prompts + memories live here) ---------------------------
def test_init_creates_owner_only_data_dir_and_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "fresh" / "sup-mem"  # does not exist yet
    cfg = load_config(overrides={"data_dir": str(data_dir)})
    rc = commands.cmd_init(
        cfg,
        clients=["claude"],
        claude_dir=tmp_path / "claude",
        claude_json=tmp_path / ".claude.json",
        use_cli=False,
    )
    assert rc == 0
    assert _mode(data_dir) == 0o700
    assert _mode(cfg.db_path) == 0o600
    Ledger(cfg.ledger_db_path).close()
    assert _mode(cfg.ledger_db_path) == 0o600


def test_sqlite_side_files_inherit_the_private_mode(config: Config) -> None:
    backend = get_backend(config)
    try:
        backend.store("a memory worth keeping private", {"source": "s"})
        wal = config.db_path.with_name(config.db_path.name + "-wal")
        assert _mode(config.db_path) == 0o600
        if wal.exists():  # WAL mode: SQLite copies the db file's permissions
            assert _mode(wal) == 0o600
    finally:
        backend.close()


def test_existing_data_files_keep_their_mode(config: Config) -> None:
    # We only set modes on files we create — a pre-existing file is its owner's call.
    config.data_dir.chmod(0o755)
    Ledger(config.ledger_db_path).close()
    get_backend(config).close()
    config.db_path.chmod(0o644)
    config.ledger_db_path.chmod(0o644)
    get_backend(config).close()
    Ledger(config.ledger_db_path).close()
    assert _mode(config.db_path) == 0o644
    assert _mode(config.ledger_db_path) == 0o644
    assert _mode(config.data_dir) == 0o755


def test_retrieval_log_is_created_private(config: Config) -> None:
    log = config.retrieval_log_path
    user_prompt_submit._log(config, "my secret prompt", [], "skip", session_id="s")
    assert log.exists() and _mode(log) == 0o600

    log.chmod(0o644)  # an existing log keeps its mode (only creation sets it)
    user_prompt_submit._log(config, "second prompt", [], "skip", session_id="s")
    assert _mode(log) == 0o644
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2  # still appending


# --- `setup qdrant`: compose file is always ours, private, and cleaned up ---------------------
def test_compose_up_runs_only_its_own_private_compose_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    planted = "services:\n  evil:\n    image: evil/evil\n"
    (cwd / "docker-compose.qdrant.yml").write_text(planted, encoding="utf-8")  # CWD bait
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    (tmp_dir / "sup-mem-docker-compose.qdrant.yml").write_text(planted, encoding="utf-8")  # old
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/bin/docker")

    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        path = Path(argv[argv.index("-f") + 1])
        seen.update(path=path, text=path.read_text(encoding="utf-8"), mode=_mode(path))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    commands._compose_up(Console(file=io.StringIO()))

    assert seen["text"] == commands._COMPOSE_YAML  # never the planted file's content
    assert seen["path"].name != "sup-mem-docker-compose.qdrant.yml"
    assert seen["path"].parent == tmp_dir and seen["mode"] == 0o600
    assert not seen["path"].exists()  # cleaned up
    assert (tmp_dir / "sup-mem-docker-compose.qdrant.yml").read_text() == planted  # untouched


def test_compose_up_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/bin/docker")

    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", failing_run)
    out = io.StringIO()
    commands._compose_up(Console(file=out))
    assert "could not start Qdrant" in out.getvalue()
    assert list(tmp_dir.iterdir()) == []
