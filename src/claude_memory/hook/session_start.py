"""SessionStart hook — inject the scale-aware topic manifest once per session (§6.6).

Not the per-prompt hot path, so importing the backend + manifest here is fine. Still fails
open and silent (exit 0) if anything goes wrong.
"""

from __future__ import annotations

import contextlib
import sys

from claude_memory.config import load_config


def main() -> int:
    with contextlib.suppress(Exception):
        sys.stdin.read()  # drain the hook payload (unused today)
        config = load_config()

        from claude_memory.backends import get_backend
        from claude_memory.manifest import build_manifest

        backend = get_backend(config)
        try:
            text = build_manifest(backend, config)
        finally:
            backend.close()

        if text.strip():
            sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
