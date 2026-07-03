"""sup-mem: a self-hosted, pluggable global memory layer for Claude.

Two front-doors (an automatic hook + explicit MCP tools) over one pluggable backend (I1).

Keep this module import-light: the per-prompt hook imports from the package on the hot path
(I2), so we import only stdlib-backed modules here (``config``/``models``) and NEVER touch
``backends`` or ``embedding`` at import time.
"""

from __future__ import annotations

from sup_mem.config import Config, load_config
from sup_mem.models import Hit, MemoryRecord

__all__ = ["Config", "Hit", "MemoryRecord", "__version__", "load_config"]


def __getattr__(name: str) -> str:
    # Lazy __version__ (PEP 562): importing importlib.metadata costs ~30 ms, and the per-prompt
    # hook imports this package (via sup_mem.config) but never reads the version. Resolve it only
    # when actually accessed (CLI --version, status) so the hot path stays import-light (I2).
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("sup-mem")
        except PackageNotFoundError:  # running from a source tree without an install
            return "0.0.0"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
