"""sup-mem performance benchmark — percentiles, retrieval scaling, startup decomposition.

Run: ``uv run python bench/bench.py``

Hermetic: seeds throwaway stores in tmp dirs with retrieval logging OFF, so a developer's real
``~/.sup-mem`` store and logs are never touched. Measures what a user actually pays per prompt.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

# Run the hot-path hook in a fresh interpreter (what the host spawns per prompt), using the
# current source tree — no dependency on an installed console script.
_HOOK_SRC = "from sup_mem.hook.user_prompt_submit import main; raise SystemExit(main())"
HOOK_CMD = [sys.executable, "-c", _HOOK_SRC]

CORPUS = [
    "the {svc}-api is rolled out with a blue-green strategy behind the load balancer",
    "the {svc} postgres connection pool is capped at fifty at peak load",
    "a nightly reconciliation job runs at 02:00 UTC for the {svc} service",
    "staging for {svc} mirrors production but uses a single-AZ instance to cut spend",
    "feature rollouts for {svc} are gated through a dashboard, not env vars",
]
SVCS = ["payments", "orders", "billing", "auth", "search", "notify", "ledger", "report"]


def pctile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def row(name: str, ms: list[float]) -> None:
    print(
        f"  {name:<32} n={len(ms):>3}  p50={pctile(ms, 50):6.1f}  p90={pctile(ms, 90):6.1f}  "
        f"p99={pctile(ms, 99):6.1f}  mean={statistics.mean(ms):6.1f}  min={min(ms):6.1f}  ms"
    )


def seed(data_dir: str, n: int) -> None:
    from sup_mem.backends import get_backend
    from sup_mem.config import load_config

    cfg = load_config(overrides={"data_dir": data_dir, "backend": "sqlite_fts"})
    backend = get_backend(cfg)
    for i in range(n):
        text = CORPUS[i % len(CORPUS)].format(svc=SVCS[i % len(SVCS)]) + f" (record {i})"
        backend.store(text, {"source": f"s{i}", "tags": [f"topic{i % 30}"]})
    backend.close()


def bench_cold_hook(n: int = 40) -> list[float]:
    data_dir = tempfile.mkdtemp()
    seed(data_dir, 42)  # a representative small store
    env = {**os.environ, "SUP_MEM_DATA_DIR": data_dir, "SUP_MEM_LOGGING_RETRIEVAL_LOG": "false"}
    stdin = json.dumps(
        {"prompt": "how do we deploy the payments service without downtime", "session_id": "b"}
    ).encode()
    samples: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        subprocess.run(
            HOOK_CMD, input=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
        )
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def bench_cmd(args: list[str], n: int = 20) -> list[float]:
    samples: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def bench_fts_scale() -> None:
    from sup_mem.backends import get_backend
    from sup_mem.config import load_config

    for size in (100, 1_000, 10_000):
        data_dir = tempfile.mkdtemp()
        seed(data_dir, size)
        cfg = load_config(overrides={"data_dir": data_dir, "backend": "sqlite_fts"})
        backend = get_backend(cfg)
        backend.search("deploy payments blue-green pool region", k=3, threshold=0.0)  # warm
        samples: list[float] = []
        for _ in range(60):
            start = time.perf_counter()
            backend.search("deploy payments service without downtime pool", k=3, threshold=0.8)
            samples.append((time.perf_counter() - start) * 1000)
        backend.close()
        row(f"FTS search @ {size:>5} mem", samples)


def main() -> None:
    print("\n== startup decomposition (cold subprocess) ==")
    row("python -c pass (interp floor)", bench_cmd([sys.executable, "-c", "pass"]))
    row("+ import sup_mem.config", bench_cmd([sys.executable, "-c", "import sup_mem.config"]))
    print("\n== per-prompt hot path (fresh interpreter, 42-mem store) ==")
    row("UserPromptSubmit hook (cold)", bench_cold_hook(40))
    print("\n== retrieval scaling (in-process FTS search, warm) ==")
    bench_fts_scale()


if __name__ == "__main__":
    main()
