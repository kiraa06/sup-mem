"""Latency budgets (HANDOVER §8, §11.5). Marked slow; bounds are generous to avoid CI flakiness
while still catching gross regressions. Measured values are printed for tuning."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable

import pytest

from sup_mem.backends import get_backend
from sup_mem.config import Config
from sup_mem.hook import user_prompt_submit as hook


@pytest.mark.slow
def test_fts_query_latency_on_10k_records(make_config: Callable[..., Config]) -> None:
    cfg = make_config(backend="sqlite_fts")
    b = get_backend(cfg)
    try:
        for i in range(10_000):
            b.store(
                f"memory {i} about topic {i % 50}, service {i % 20}, and region {i % 7}",
                {"source": f"s{i}", "tags": [f"topic{i % 50}"]},
            )
        b.search("service topic region", k=6, threshold=0.0)  # warm caches
        runs = 25
        start = time.perf_counter()
        for _ in range(runs):
            b.search("service topic region memory 1234", k=6, threshold=0.35)
        avg_ms = (time.perf_counter() - start) / runs * 1000
        print(f"FTS query avg {avg_ms:.2f} ms over 10k records (budget 10 ms)")
        assert avg_ms < 50  # generous; the target is <10 ms
    finally:
        b.close()


@pytest.mark.slow
def test_tier1_skip_is_cheap(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUP_MEM_LOGGING_RETRIEVAL_LOG", "false")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "thanks!"})))
    hook.main()  # warm imports
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "thanks again!"})))
    start = time.perf_counter()
    hook.main()
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"tier-1 skip logic {elapsed_ms:.3f} ms (budget <5 ms; backend never imported)")
    assert elapsed_ms < 25  # the Tier-1 gate is regex-only; no backend/model touched
