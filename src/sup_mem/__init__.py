"""sup-mem: a self-hosted, pluggable global memory layer for Claude.

Two front-doors (an automatic hook + explicit MCP tools) over one pluggable backend (I1).

Keep this module import-light: the per-prompt hook imports from the package on the hot path
(I2), so we import only stdlib-backed modules here (``config``/``models``) and NEVER touch
``backends`` or ``embedding`` at import time.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from sup_mem.config import Config, load_config
from sup_mem.models import Hit, MemoryRecord

try:
    __version__ = version("sup-mem")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["Config", "Hit", "MemoryRecord", "__version__", "load_config"]
