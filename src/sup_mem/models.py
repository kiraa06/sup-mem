"""Core data models shared by every backend (HANDOVER §6.1).

These are deliberately plain dataclasses with no third-party deps so they can be imported on
the hook's hot path (I2) and used by the zero-optional-deps default install (I8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Free-form, JSON-serializable metadata attached to a memory (tags, source, session, ...).
Metadata = dict[str, Any]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Hit:
    """A single search result.

    ``score`` MUST be normalized to 0..1 across ALL backends so the retrieval ``threshold``
    means the same thing regardless of backend (§6.1). Best (most relevant) first.
    """

    id: str
    text: str
    score: float
    metadata: Metadata = field(default_factory=dict)


@dataclass(slots=True)
class MemoryRecord:
    """A durable memory as persisted by a backend."""

    id: str
    text: str
    metadata: Metadata = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @property
    def tags(self) -> list[str]:
        """Normalized tag list read from ``metadata['tags']`` (accepts a list or CSV string)."""
        raw = self.metadata.get("tags", [])
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        if isinstance(raw, list):
            return [str(t) for t in raw if str(t).strip()]
        return []
