"""migrate-native: copy Claude Code's built-in file memories into the store."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from claude_memory import commands
from claude_memory.backends import get_backend
from claude_memory.config import Config
from claude_memory.migrate import iter_native_memories, migrate_native

FACT_MD = """---
name: canonical-jenkins-job
description: The canonical Jenkins job for ETS java17 deployments
metadata:
  type: feedback
---

Use the declarative pipeline job, not the stale freestyle one.

**Why:** the freestyle job has a latent OTel bug.
"""

BARE_MD = "A frontmatterless note about the staging bastion host."


def _fake_projects(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    proj_a = projects / "-Users-x-Documents-proj-a" / "memory"
    proj_b = projects / "-Users-x-Documents-proj-b" / "memory"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    (proj_a / "MEMORY.md").write_text("- [index](fact.md)")  # index → must be skipped
    (proj_a / "fact.md").write_text(FACT_MD)
    (proj_a / "empty.md").write_text("---\nname: hollow\n---\n")  # no body/description
    (proj_b / "bare.md").write_text(BARE_MD)  # no frontmatter → defaults
    return projects


def test_iter_parses_frontmatter_and_skips_index(tmp_path: Path) -> None:
    memories = {m.path.name: m for m in iter_native_memories(_fake_projects(tmp_path))}
    assert set(memories) == {"fact.md", "empty.md", "bare.md"}  # MEMORY.md excluded

    fact = memories["fact.md"]
    assert fact.project == "proj-a"
    assert fact.name == "canonical-jenkins-job"
    assert fact.kind == "feedback"
    assert fact.description.startswith("The canonical Jenkins job")
    assert "declarative pipeline" in fact.body
    assert fact.text.startswith(fact.description)  # description leads the stored text
    assert fact.source == "native:proj-a/fact.md"

    bare = memories["bare.md"]
    assert bare.name == "bare" and bare.kind == "project"  # defaults
    assert bare.text == BARE_MD


def test_migrate_stores_with_provenance_and_is_idempotent(
    tmp_path: Path, make_config: Callable[..., Config]
) -> None:
    projects = _fake_projects(tmp_path)
    backend = get_backend(make_config(backend="sqlite_fts"))
    try:
        report = migrate_native(backend, projects)
        assert len(report["migrated"]) == 2  # fact + bare; empty skipped
        assert report["skipped_empty"] == ["native:proj-a/empty.md"]
        assert report["new"] == 2 and report["total"] == 2

        hits = backend.search("canonical jenkins job for ets deployments", k=3, threshold=0.0)
        assert hits and hits[0].metadata["source"] == "native:proj-a/fact.md"
        assert hits[0].metadata["topic"] == "canonical-jenkins-job"
        assert "feedback" in hits[0].metadata["tags"]

        rerun = migrate_native(backend, projects)  # idempotent: nothing new
        assert rerun["new"] == 0 and rerun["total"] == 2
    finally:
        backend.close()


def test_dry_run_stores_nothing(tmp_path: Path, make_config: Callable[..., Config]) -> None:
    backend = get_backend(make_config(backend="sqlite_fts"))
    try:
        report = migrate_native(backend, _fake_projects(tmp_path), dry_run=True)
        assert len(report["migrated"]) == 2
        assert backend.health()["count"] == 0
    finally:
        backend.close()


def test_cmd_handles_missing_projects_dir(
    tmp_path: Path, make_config: Callable[..., Config]
) -> None:
    rc = commands.cmd_migrate_native(
        make_config(backend="sqlite_fts"), projects_dir=tmp_path / "nope"
    )
    assert rc == 0  # friendly no-op, not an error


def test_cmd_end_to_end(tmp_path: Path, make_config: Callable[..., Config]) -> None:
    cfg = make_config(backend="sqlite_fts")
    rc = commands.cmd_migrate_native(cfg, projects_dir=_fake_projects(tmp_path))
    assert rc == 0
    backend = get_backend(cfg)
    try:
        assert backend.health()["count"] == 2
    finally:
        backend.close()
