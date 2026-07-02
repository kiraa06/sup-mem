"""Seed the FICTIONAL demo store used by docs/demo.tape.

Everything here is invented (ACME-style) — no real org names, accounts, hosts, or people.
Run before re-rendering the README gif:

    uv run python docs/demo_seed.py && vhs docs/demo.tape
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from sup_mem.backends import get_backend
from sup_mem.config import load_config

DEMO_DIR = Path("/tmp/supmem-demo")


def main() -> None:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    cfg = load_config(overrides={"data_dir": str(DEMO_DIR)})
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.pinned_facts_path.write_text(
        "- ACME platform team; prefers concise answers.\n"
        "- Staging deploys are blue-green via deploy.sh.\n",
        encoding="utf-8",
    )

    backend = get_backend(cfg)
    old = backend.store(
        "payments-api pod restarts believed to be caused by OOM kills on the java17 image.",
        {"source": "native:runbook/payments-api.md", "tags": ["pods"]},
    )
    new = backend.store(
        "payments-api pod restarts are caused by duplicate BouncyCastle jars tripping a "
        "Tomcat annotation-scan StackOverflow on the new base image.",
        {"source": "native:runbook/payments-api.md", "tags": ["pods"]},
    )
    backend.store(
        "The checkout service rate limit is 400 rps per region, decided in the June review.",
        {"source": "mcp:remember", "tags": ["limits"]},
    )
    backend.close()

    # Space the supersession out so the --as-of shot has legible dates.
    conn = sqlite3.connect(str(DEMO_DIR / "memory.db"))
    with conn:
        conn.execute(
            "UPDATE memories SET recorded_at='2026-06-01T09:00:00+00:00', "
            "valid_from='2026-06-01T09:00:00+00:00', "
            "superseded_at='2026-06-29T14:00:00+00:00' WHERE id=?",
            (old,),
        )
        conn.execute(
            "UPDATE memories SET recorded_at='2026-06-29T14:00:00+00:00', "
            "valid_from='2026-06-29T14:00:00+00:00' WHERE id=?",
            (new,),
        )
    conn.close()
    print(f"demo store seeded at {DEMO_DIR} ({old[:8]} superseded by {new[:8]})")


if __name__ == "__main__":
    main()
