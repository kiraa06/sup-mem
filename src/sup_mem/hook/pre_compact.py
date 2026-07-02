"""PreCompact hook — capture what matters before compaction destroys it (PHASE10).

Claude Code fires this before manual `/compact` and auto-compaction, passing ``session_id``
+ ``transcript_path`` + ``trigger`` on stdin. Silent, fail-open, exit 0 always (C2). The
heavy part (one headless `claude -p` call) is the whole point — bounded by
``capture.timeout_seconds`` and the hook's registered timeout.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


def main() -> int:
    from sup_mem.capture import CAPTURE_ENV_MARKER

    if os.environ.get(CAPTURE_ENV_MARKER):
        return 0  # we ARE the extractor's child session — never recurse (C4)
    with contextlib.suppress(Exception):
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return 0
        session_id = str(data.get("session_id", ""))
        transcript_path = str(data.get("transcript_path", ""))
        trigger = str(data.get("trigger", ""))
        if not session_id or not transcript_path:
            return 0

        from sup_mem.config import load_config

        config = load_config()
        if not config.capture.enabled:
            return 0

        from sup_mem.capture import run_capture

        run_capture(session_id, Path(transcript_path), config, trigger=trigger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
