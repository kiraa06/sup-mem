"""Stop hook — closes the outcome loop after every completed response (PHASE6, L1).

Claude Code fires this when a response finishes, passing ``session_id`` + ``transcript_path``
on stdin. We ingest this session's new retrieval-log lines into the ledger, parse the
transcript, and attribute each injected memory as referenced / ignored / contradicted.

NOT the per-prompt hot path, but still: lazy imports, no model, silent, always exit 0 (L2).
Budget < 500 ms on a 5 MB transcript.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path


def main() -> int:
    import os

    if os.environ.get("SUP_MEM_CAPTURE"):
        return 0  # inside the PreCompact extractor's child session (PHASE10 C4)
    with contextlib.suppress(Exception):
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return 0
        session_id = str(data.get("session_id", ""))
        transcript_path = str(data.get("transcript_path", ""))
        if not session_id or not transcript_path:
            return 0

        from sup_mem.config import load_config

        config = load_config()
        if not config.ledger.enabled:
            return 0

        from sup_mem.ledger import Ledger, parse_transcript

        with Ledger(config.ledger_db_path) as ledger:
            ledger.ingest_log(session_id, config.retrieval_log_path)

            # Only fetch texts for this session's still-unattributed injections.
            pending_ids = ledger.pending_injected_ids(session_id)

            texts: dict[str, str] = {}
            if pending_ids:
                from sup_mem.backends import get_backend

                backend = get_backend(config)
                try:
                    texts = backend.fetch(pending_ids)
                finally:
                    backend.close()

            turns = parse_transcript(Path(transcript_path))
            if turns:
                ledger.attribute(session_id, turns, texts, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
