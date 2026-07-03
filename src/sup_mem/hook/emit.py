"""Dialect-aware context injection for the two injecting hooks (UserPromptSubmit, SessionStart).

Ultra-light on purpose — imports only ``os``/``json``/``sys`` so the per-prompt hot path stays
import-clean (I2; the §11.2 subprocess test guards this). It must never pull in the ``clients``
package or anything heavier.

Claude Code and Codex inject raw stdout as context; Gemini honors only the
``hookSpecificOutput.additionalContext`` JSON envelope. The active host is read from
``SUP_MEM_CLIENT`` (absent → Claude Code, plain text — unchanged from before multi-client).
"""

from __future__ import annotations

import json
import os
import sys


def emit_context(text: str) -> None:
    if not text:
        return
    if os.environ.get("SUP_MEM_CLIENT", "").strip().lower() in ("gemini", "antigravity"):
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"additionalContext": text}}) + "\n")
    else:
        sys.stdout.write(text + "\n")
