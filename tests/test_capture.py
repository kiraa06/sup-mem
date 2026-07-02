"""PreCompact capture (PHASE10 acceptance 1–5). Hermetic: a fake `claude` — zero tokens."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sup_mem import capture
from sup_mem.backends import get_backend
from sup_mem.config import Config
from sup_mem.hook import pre_compact

SESSION = "sess-cap-1"

FACTS_JSON = json.dumps(
    [
        {
            "text": "The team decided to pin the payments-api base image to java17-tomcat9.",
            "topic": "payments-base-image",
            "tags": ["decision"],
        },
        {
            "text": "Kiran prefers evidence-based tuning over hand-picked thresholds.",
            "topic": "tuning-preference",
            "tags": ["preference"],
        },
    ]
)


def _transcript(path: Path, n_turns: int = 6) -> Path:
    lines = []
    for i in range(n_turns):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": f"user turn {i} about deploys " * 20},
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": f"assistant reply {i} with detail " * 20}
                        ],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fake_runner(
    stdout: str, returncode: int = 0, capture_env: dict[str, Any] | None = None
) -> Any:
    def run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        if capture_env is not None:
            capture_env["cmd"] = cmd
            capture_env["env"] = kw.get("env") or {}
            capture_env["input"] = kw.get("input", "")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return run


def _fake_claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`extract_with_claude` probes shutil.which('claude') — give it something to find."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


# --- extraction + storage ------------------------------------------------------------------
def test_capture_stores_topic_keyed_facts(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude_on_path(tmp_path, monkeypatch)
    seen: dict[str, Any] = {}
    n = capture.run_capture(
        SESSION,
        _transcript(tmp_path / "t.jsonl"),
        config,
        trigger="auto",
        runner=_fake_runner(FACTS_JSON, capture_env=seen),
    )
    assert n == 2
    assert seen["env"].get("SUP_MEM_CAPTURE") == "1"  # recursion marker on the child (C4)
    assert "USER:" in seen["input"] and "ASSISTANT:" in seen["input"]

    backend = get_backend(config)
    try:
        hits = backend.search("payments-api base image pinned", k=3, threshold=0.0)
        assert hits and "java17-tomcat9" in hits[0].text
        assert hits[0].metadata["source"] == f"session:{SESSION}:payments-base-image"
        assert "auto-capture" in hits[0].metadata["tags"]
    finally:
        backend.close()
    log = (config.logs_dir / "capture.log").read_text()
    assert '"stored"' in log and SESSION in log


def test_recompaction_supersedes_same_topic(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude_on_path(tmp_path, monkeypatch)
    t = _transcript(tmp_path / "t.jsonl")
    capture.run_capture(SESSION, t, config, runner=_fake_runner(FACTS_JSON))
    updated = json.dumps(
        [
            {
                "text": "The payments-api base image decision changed: now pinned to java21.",
                "topic": "payments-base-image",
                "tags": ["decision"],
            }
        ]
    )
    capture.run_capture(SESSION, t, config, runner=_fake_runner(updated))

    backend = get_backend(config)
    try:
        health = backend.health()
        assert health["count"] == 2  # java21 version + the preference fact
        assert health["versions"] == 3  # the java17 version survives as history (C3)
        live = backend.search("payments base image", k=3, threshold=0.0)
        assert "java21" in live[0].text
    finally:
        backend.close()


def test_batch_topic_collision_gets_suffix(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude_on_path(tmp_path, monkeypatch)
    dup = json.dumps(
        [
            {"text": "First distinct fact about deploy windows in staging.", "topic": "deploys"},
            {"text": "Second distinct fact about deploy freezes on Fridays.", "topic": "deploys"},
        ]
    )
    capture.run_capture(
        SESSION, _transcript(tmp_path / "t.jsonl"), config, runner=_fake_runner(dup)
    )
    backend = get_backend(config)
    try:
        assert backend.health()["count"] == 2  # suffix kept both fact lines alive
    finally:
        backend.close()


# --- fail-open + guards (C2/C4/C5) ----------------------------------------------------------
def test_extractor_failure_stores_nothing(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude_on_path(tmp_path, monkeypatch)
    for bad in (_fake_runner("", returncode=1), _fake_runner("no json here at all")):
        assert (
            capture.run_capture(SESSION, _transcript(tmp_path / "t.jsonl"), config, runner=bad) == 0
        )
    backend = get_backend(config)
    try:
        assert backend.health()["count"] == 0
    finally:
        backend.close()


def test_missing_claude_binary_is_silent(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no `claude` anywhere
    n = capture.run_capture(SESSION, _transcript(tmp_path / "t.jsonl"), config)
    assert n == 0


def test_hook_recursion_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUP_MEM_CAPTURE", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert pre_compact.main() == 0
    from sup_mem.hook import session_start, stop, user_prompt_submit

    for hook_main in (user_prompt_submit.main, session_start.main, stop.main):
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        assert hook_main() == 0  # instant no-op inside the extractor child (C4)


def test_hook_fails_open_on_garbage(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(config.data_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO("{{{not json"))
    assert pre_compact.main() == 0
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "s", "transcript_path": "/nope.jsonl"})),
    )
    assert pre_compact.main() == 0


def test_disabled_capture_short_circuits(
    make_config: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(capture={"enabled": False})
    monkeypatch.setenv("SUP_MEM_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("SUP_MEM_CAPTURE_ENABLED", "false")
    payload = json.dumps(
        {"session_id": SESSION, "transcript_path": str(_transcript(tmp_path / "t.jsonl"))}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert pre_compact.main() == 0
    backend = get_backend(cfg)
    try:
        assert backend.health()["count"] == 0
    finally:
        backend.close()


# --- parsing + rendering ---------------------------------------------------------------------
def test_parse_extraction_tolerates_fences_and_caps() -> None:
    fenced = f"Here you go:\n```json\n{FACTS_JSON}\n```\nHope that helps!"
    facts = capture.parse_extraction(fenced, max_memories=1)
    assert len(facts) == 1 and facts[0]["topic"] == "payments-base-image"
    assert capture.parse_extraction("[]", 8) == []
    assert capture.parse_extraction("total prose, no json", 8) == []
    assert capture.parse_extraction('[{"text": "too short"}]', 8) == []


def test_transcript_rendering_respects_budget(make_config: Any, tmp_path: Path) -> None:
    cfg = make_config(capture={"max_transcript_chars": 800, "per_turn_chars": 300})
    text = capture.render_transcript_tail(_transcript(tmp_path / "t.jsonl", n_turns=10), cfg)
    assert 0 < len(text) <= 800 + 320  # budget + at most one over-budget block of slack
    assert "assistant reply 9" in text.lower()  # newest turns win
    assert "turn 0 about deploys" not in text.lower()  # oldest turns dropped under budget
    assert "USER:" in text and "ASSISTANT:" in text
