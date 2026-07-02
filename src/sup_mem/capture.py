"""PreCompact capture — the compaction lifeboat (docs/PHASE10-CAPTURE.md).

Just before Claude Code compacts a session, a headless ``claude -p`` call (C1) reads the
transcript tail and returns the facts worth keeping; we store them so the very next
post-compaction prompt can get them re-injected by the UserPromptSubmit hook.

Everything fails open (C2); re-compactions supersede rather than duplicate via topic-keyed
sources (C3); the marker env var prevents recursion (C4); it costs one small-model call per
compaction and says so in the docs (C5).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sup_mem.config import Config

CAPTURE_ENV_MARKER = "SUP_MEM_CAPTURE"  # set in the extractor child; all hooks bail on it

EXTRACTION_PROMPT = (
    "You are distilling a Claude Code session moments before its context is compacted. "
    "From the conversation below, extract ONLY the durable facts worth remembering in "
    "future sessions: decisions made, stable facts about the user's systems and "
    "preferences, corrections the user issued, and hard-won lessons. Do NOT extract "
    "transient progress, tool noise, code that lives in the repo, or anything trivial.\n\n"
    "Return STRICT JSON only — an array of at most {max_memories} objects, each "
    '{{"text": "<one self-contained paragraph>", "topic": "<short-kebab-slug>", '
    '"tags": ["<1-3 short tags>"]}}. Return [] if nothing qualifies. No prose outside JSON.'
)

# Injectable for tests; matches subprocess.run's shape.
Runner = Any


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "fact"


def render_transcript_tail(transcript_path: Path, config: Config) -> str:
    """Newest main-chain turns rendered as USER:/ASSISTANT: text, within the char budget."""
    from sup_mem.ledger import parse_transcript

    turns = parse_transcript(transcript_path)
    if not turns:
        return ""
    budget = config.capture.max_transcript_chars
    per_turn = config.capture.per_turn_chars
    rendered: list[str] = []
    used = 0
    for turn in reversed(turns):
        text = turn.text[:per_turn]
        block = f"{turn.role.upper()}: {text}"
        if used + len(block) > budget and rendered:
            break
        rendered.append(block)
        used += len(block)
    return "\n\n".join(reversed(rendered))


def parse_extraction(raw: str, max_memories: int) -> list[dict[str, Any]]:
    """Parse the extractor's reply defensively: strict JSON preferred, fences tolerated."""
    text = raw.strip()
    if "```" in text:  # tolerate ```json fenced replies
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if blocks:
            text = blocks[0].strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    facts: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        fact_text = str(item.get("text", "")).strip()
        if len(fact_text) < 20:
            continue  # too short to be a durable fact
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        facts.append(
            {
                "text": fact_text,
                "topic": _slug(str(item.get("topic", "")) or fact_text[:40]),
                "tags": [str(t)[:32] for t in tags[:3]],
            }
        )
        if len(facts) >= max_memories:
            break
    return facts


def extract_with_claude(
    transcript_text: str, config: Config, runner: Runner = subprocess.run
) -> list[dict[str, Any]]:
    """One headless small-model call (C1/C5). Empty list on any failure (C2)."""
    if shutil.which("claude") is None:
        return []
    import os

    prompt = EXTRACTION_PROMPT.format(max_memories=config.capture.max_memories)
    child_env = {**os.environ, CAPTURE_ENV_MARKER: "1"}  # recursion guard (C4)
    try:
        proc = runner(
            ["claude", "-p", prompt, "--model", config.capture.model],
            input=transcript_text,
            capture_output=True,
            text=True,
            timeout=config.capture.timeout_seconds,
            env=child_env,
        )
    except Exception:
        return []
    if int(proc.returncode) != 0:
        return []
    return parse_extraction(str(proc.stdout or ""), config.capture.max_memories)


def store_facts(facts: list[dict[str, Any]], session_id: str, config: Config) -> list[str]:
    """Store with topic-keyed sources so re-compactions supersede stale extractions (C3)."""
    if not facts:
        return []
    from sup_mem.backends import get_backend

    backend = get_backend(config)
    stored: list[str] = []
    seen_topics: dict[str, int] = {}
    try:
        for fact in facts:
            topic = fact["topic"]
            count = seen_topics.get(topic, 0)
            seen_topics[topic] = count + 1
            if count:
                topic = f"{topic}-{count + 1}"  # batch-internal collision → distinct fact line
            stored.append(
                backend.store(
                    fact["text"],
                    {
                        "source": f"session:{session_id}:{topic}",
                        "topic": topic,
                        "tags": [*fact["tags"], "auto-capture"],
                        "session_id": session_id,
                    },
                )
            )
    finally:
        backend.close()
    return stored


def _log_capture(config: Config, record: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        with (config.logs_dir / "capture.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def run_capture(
    session_id: str,
    transcript_path: Path,
    config: Config,
    trigger: str = "",
    runner: Runner = subprocess.run,
) -> int:
    """The PreCompact entry: render → extract → store → log. Returns stored count."""
    started = datetime.now(UTC)
    transcript_text = render_transcript_tail(transcript_path, config)
    if len(transcript_text) < 500:
        return 0  # nothing substantial to distill
    facts = extract_with_claude(transcript_text, config, runner=runner)
    stored = store_facts(facts, session_id, config)
    _log_capture(
        config,
        {
            "ts": started.isoformat(),
            "session_id": session_id,
            "trigger": trigger,
            "transcript_chars": len(transcript_text),
            "extracted": len(facts),
            "stored": stored,
            "seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
        },
    )
    return len(stored)
