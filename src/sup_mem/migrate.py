"""Migrate Claude Code's built-in file memories into sup-mem.

Claude Code's native memory lives at ``~/.claude/projects/<project-slug>/memory/*.md`` — one
fact per file with frontmatter (``name`` / ``description`` / ``metadata.type``) plus a
``MEMORY.md`` index per project. This copies those facts into the sup-mem store with
full provenance (source file, topic, type + project tags) so the hook and ``recall`` work
over them.

Copy-only: source files are never modified or deleted. Idempotent: the backend dedupes on
``(text, source)``, so re-running migrates only files that are new or changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sup_mem.backends.base import MemoryBackend

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_INDEX_FILENAME = "MEMORY.md"


@dataclass(frozen=True)
class NativeMemory:
    """One parsed native-memory file."""

    project: str
    path: Path
    name: str
    description: str
    kind: str  # frontmatter metadata.type: user | feedback | project | reference
    body: str

    @property
    def text(self) -> str:
        """Stored text: the one-line description leads so it is searchable too."""
        return "\n\n".join(part for part in (self.description, self.body) if part).strip()

    @property
    def source(self) -> str:
        return f"native:{self.project}/{self.path.name}"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "topic": self.name,
            "tags": [self.kind, self.project, self.name],
            "migrated_from": str(self.path),
        }


def _frontmatter_field(frontmatter: str, pattern: str) -> str:
    found = re.search(pattern, frontmatter, re.MULTILINE)
    return found.group(1).strip().strip("\"'") if found else ""


def parse_memory_file(path: Path, project: str) -> NativeMemory:
    raw = path.read_text(encoding="utf-8")
    name, description, kind = "", "", ""
    body = raw
    match = _FRONTMATTER_RE.match(raw)
    if match:
        body = raw[match.end() :]
        frontmatter = match.group(1)
        name = _frontmatter_field(frontmatter, r"^name:\s*(.+)$")
        description = _frontmatter_field(frontmatter, r"^description:\s*(.+)$")
        kind = _frontmatter_field(frontmatter, r"^\s+type:\s*(.+)$")  # nested under metadata:
    return NativeMemory(
        project=project,
        path=path,
        name=name or path.stem,
        description=description,
        kind=kind or "project",
        body=body.strip(),
    )


def project_label(memory_dir: Path) -> str:
    """Human-ish project name from the slug dir, e.g.
    ``-Users-kiranjose-Documents-aws-cost-app`` → ``aws-cost-app``."""
    slug = memory_dir.parent.name
    return slug.rsplit("-Documents-", 1)[-1].lstrip("-") or slug


def iter_native_memories(projects_dir: Path) -> Iterator[NativeMemory]:
    """Yield every parseable native memory under ``<projects_dir>/*/memory/*.md``."""
    for memory_dir in sorted(projects_dir.glob("*/memory")):
        if not memory_dir.is_dir():
            continue
        project = project_label(memory_dir)
        for path in sorted(memory_dir.glob("*.md")):
            if path.name == _INDEX_FILENAME:
                continue  # per-project index, not a memory
            try:
                yield parse_memory_file(path, project)
            except (OSError, UnicodeDecodeError):
                continue  # unreadable file — skip rather than abort the migration


def migrate_native(
    backend: MemoryBackend, projects_dir: Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """Copy native memories into ``backend``. Returns a report dict.

    Report keys: ``migrated`` (list of (source, kind, chars)), ``skipped_empty`` (list of
    source), ``new`` (int, records actually added — 0 for a re-run), ``total`` (store count
    after, or -1 on dry runs).
    """
    migrated: list[tuple[str, str, int]] = []
    skipped: list[str] = []
    before = int(backend.health().get("count", 0)) if not dry_run else 0
    for memory in iter_native_memories(projects_dir):
        if not memory.text:
            skipped.append(memory.source)
            continue
        if not dry_run:
            backend.store(memory.text, memory.metadata)
        migrated.append((memory.source, memory.kind, len(memory.text)))
    if dry_run:
        return {"migrated": migrated, "skipped_empty": skipped, "new": 0, "total": -1}
    after = int(backend.health().get("count", 0))
    return {
        "migrated": migrated,
        "skipped_empty": skipped,
        "new": after - before,
        "total": after,
    }
