"""Configuration for sup-mem.

Precedence (low → high): dataclass defaults ← ``config.toml`` ← environment ← explicit flags.

Every tunable from HANDOVER §8 lives here with a sane default; nothing operational is
hard-coded in logic (I9). Env vars mirror the nested keys, upper-cased and joined with ``_``
under the ``SUP_MEM_`` prefix, e.g. ``SUP_MEM_RETRIEVAL_THRESHOLD`` and
``SUP_MEM_QDRANT_HNSW_M`` (§13).

This module is stdlib-only so it is safe to import on the hook's hot path (I2).
"""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, cast, get_origin, get_type_hints

ENV_PREFIX = "SUP_MEM_"
DEFAULT_DATA_DIR = Path.home() / ".sup-mem"


@dataclass
class RetrievalConfig:
    k: int = 3
    threshold: float = 0.35  # a dial to tune on your own data; see retrieval logging (§8)
    # Per-memory injection cap for the HOOK (chars; 0 = unlimited). Long memories inject a
    # clipped head + a "use recall" tail; explicit `recall` always serves the full text.
    max_inject_chars: int = 600


@dataclass
class Tier1Config:
    """Tier-1 skip-gate patterns (I3). Regex is a SKIP gate only — never a relevance gate."""

    # Whitelist of obviously-trivial openings. A match makes the turn a skip *candidate*.
    skip_patterns: list[str] = field(
        default_factory=lambda: [
            r"^\s*(hi|hey|hello|yo|sup|hiya|howdy)\b",
            r"^\s*good\s+(morning|afternoon|evening|night)\b",
            r"^\s*(thanks|thank you|thx|ty|cheers|ta)\b",
            r"^\s*(ok|okay|k|kk|cool|nice|great|perfect|awesome|sweet|got it|gotcha)\b",
            r"^\s*(yes|yep|yeah|yup|no|nope|nah|sure|fine)\b",
            r"^\s*(bye|goodbye|see you|see ya|later|cya)\b",
            r"^\s*(lol|haha|nice one)\s*$",
        ]
    )
    # Never-skip cues: if any matches, the turn is retrieved even if a skip pattern also hit.
    # These target references to PRIOR work (possessives, definite articles, past tense, keys).
    cue_patterns: list[str] = field(
        default_factory=lambda: [
            r"\b[A-Z][A-Z0-9]+-\d+\b",  # ticket keys, e.g. DEVOPS-1234
            r"\b(we|you|i)\s+(did|fixed|changed|added|built|made|discussed|decided|talked|"
            r"set\s?up|configured|deployed|wrote|created|solved|debugged|reviewed)\b",
            r"\bthe\s+\w+\s+(we|you|i)\b",  # "the fix we ...", "the thing you ..."
            r"\b(that|the|our|this)\s+(fix|ticket|issue|bug|pr|change|deploy|deployment|"
            r"config|setup|decision|approach|project|alarm|script|migration|incident|repo)\b",
            r"\b(my|our|your)\s+\w+",  # possessives referencing prior artifacts
            r"\b(earlier|previously|last time|before|yesterday|the other day|remember)\b",
        ]
    )


@dataclass
class ManifestConfig:
    """Scale-aware topic index (I10, §6.7)."""

    full_below: int = 300  # below this many records → verbatim topic list
    max_topics: int = 100  # hard cap on emitted topics
    token_budget: int = 200  # approximate cap on the injected manifest (rest → "(+N more)")
    cache: bool = True  # cache keyed on store revision; regenerate only on change


@dataclass
class ChunkingConfig:
    enabled: bool = False
    max_chars: int = 1500
    overlap: int = 150


@dataclass
class FtsConfig:
    """SQLite FTS5 / BM25 tuning + the BM25→0..1 score squash (§6.2, §8)."""

    k1: float = 1.2
    b: float = 0.75
    # Logistic squash of the (positive) BM25 relevance: s = 1/(1+exp(-steepness*(x-mid))).
    # Recall-biased defaults: BM25 magnitudes are small in small stores (low IDF), so a low
    # midpoint keeps genuine multi-term matches above the default threshold while a lone
    # common-term match stays below it. Tune with the retrieval log (§8) on your own data.
    squash_midpoint: float = 1.0
    squash_steepness: float = 1.0


@dataclass
class QdrantHnswConfig:
    m: int = 16
    ef_construct: int = 128
    ef: int = 128


@dataclass
class QdrantConfig:
    url: str = "http://localhost:6333"
    collection: str = "sup_mem"
    quantization: bool = False  # optional scalar quantization
    hnsw: QdrantHnswConfig = field(default_factory=QdrantHnswConfig)


@dataclass
class EmbeddingConfig:
    provider: str = ""  # empty → auto-detect (§6.4); pin to respect a specific model (I7)
    model: str = ""


@dataclass
class LoggingConfig:
    retrieval_log: bool = True  # log (query, ids, scores, tier) for tuning; on by default (§8)


CORRECTION_PATTERNS = (
    r"^\s*no[,.! ]",
    r"^\W*(no\W+)?(that|this|it)(.?s| is) (wrong|incorrect|outdated|stale|not right|not true"
    r"|not correct)\b",
    r"^\s*actually[, ]",
    r"\b(wrong|incorrect|outdated|stale)\b.{0,20}\bmemory\b",
    r"\bmemory\b.{0,20}\b(is|was) (wrong|incorrect|outdated|stale)\b",
)
# The pre-0.9.4 defaults, unanchored. The config template writes the list out verbatim, so an
# untouched config.toml still carries them; load_config upgrades that exact list (only).
LEGACY_CORRECTION_PATTERNS = [
    r"^\s*no[,.! ]",
    r"\bthat.?s (wrong|incorrect|outdated|stale|not right)\b",
    r"\bnot (true|correct|right)\b",
    r"\bactually[, ]",
    r"\b(wrong|incorrect|outdated)\b.{0,20}\bmemory\b",
]


@dataclass
class LedgerConfig:
    """The outcome loop (docs/PHASE6-LOOP.md): attribution, reinforcement, quarantine."""

    enabled: bool = True
    pool_k: int = 12  # candidates logged per turn (>= retrieval.k) for counterfactual tuning
    boost_weight: float = 0.10  # bounded utility boost; base relevance always dominates (L3)
    quarantine_contradictions: int = 3  # contradictions needed (and > references) to drop
    min_overlap_tokens: int = 3  # distinctive-token matches for a "referenced" verdict
    overlap_fraction: float = 0.15  # ... or this fraction of the memory's tokens, if larger
    # Chronic-ignore demotion (L3): a memory injected many times but almost never referenced is
    # burning context for nothing. Down-rank it — not a hard drop, so a strong match can still
    # win — gated on enough injections so new / rarely-queried memories are never punished.
    chronic_ignore_min_injections: int = 8
    chronic_ignore_max_ref_rate: float = 0.10
    chronic_ignore_penalty: float = 0.20
    # A correction in the user's next prompt after a referenced memory flips it to
    # contradicted. Anchored to the start of the message: "not right now" or "I actually
    # want…" mid-sentence are not corrections, and three false hits quarantine a memory.
    # NOTE: rendered as single-quoted TOML literals — keep apostrophes out of these patterns.
    correction_patterns: list[str] = field(default_factory=lambda: list(CORRECTION_PATTERNS))


@dataclass
class CaptureConfig:
    """PreCompact capture — the compaction lifeboat (docs/PHASE10-CAPTURE.md).

    COSTS TOKENS: one `capture.model` call per compaction (rare). Disable with enabled=false.
    """

    enabled: bool = True
    model: str = "haiku"  # claude CLI model alias/id for the extraction call
    max_memories: int = 8  # cap on facts stored per compaction
    max_transcript_chars: int = 60_000  # tail budget piped to the extractor
    per_turn_chars: int = 2_000  # cap per rendered turn so one giant turn can't eat the budget
    # Floor on the rendered tail: below this there is too little conversation to be worth a
    # model call. Measured need — sub-2k-char compactions extracted nothing but still paid.
    min_transcript_chars: int = 2_000
    timeout_seconds: int = 90  # extraction subprocess timeout (fail-open past it)


@dataclass
class ArchivalConfig:
    """Evidence-based cold tier with size caps (docs/PHASE9-ARCHIVAL.md A1–A6)."""

    enabled: bool = True
    main_max_mb: float = 200.0  # main DB cap; over it, decay-tier pressure archival kicks in
    archive_max_mb: float = 100.0  # archive cap; over it, FIFO PERMANENT deletion. 0 = never
    superseded_after_days: int = 90  # structural tier: superseded versions past this move
    quarantined_after_days: int = 60  # proven-harmful tier: stale quarantined memories move
    decay_min_age_days: int = 30  # pressure tier: minimum age + reference-recency guard
    keep_tag: str = "keep"  # hard opt-out tag — never archived by any regime


@dataclass
class ProvenanceConfig:
    """Tamper-evident write chain (docs/PHASE8-TEMPORAL.md T5). HMAC key at ~/.sup-mem/key."""

    enabled: bool = True


@dataclass
class MaintenanceConfig:
    """Housekeeping run by `sup-mem maintain` (scheduled via `sup-mem service install`)."""

    log_keep_days: int = 14  # retrieval.jsonl rotation window (ledger cursors are rebased)
    backup_keep: int = (
        5  # distinct-state store backups retained (unchanged stores aren't re-backed)
    )
    candidates_keep: int = 20000  # ledger `candidates` rows kept (bounds tune-replay history)
    auto_tune: bool = True  # apply tune recommendation only when lossless
    tune_min_attributed: int = 20  # minimum attributed injections before auto-tune acts
    notify: bool = True  # macOS notification when maintain finds problems
    hour: int = 3  # launchd schedule
    minute: int = 30


@dataclass
class Config:
    """Top-level, fully-resolved configuration."""

    backend: str = "sqlite_fts"  # "sqlite_fts" (default, I8) | "qdrant" | "pgvector"
    data_dir: Path = DEFAULT_DATA_DIR
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    tier1: Tier1Config = field(default_factory=Tier1Config)
    manifest: ManifestConfig = field(default_factory=ManifestConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    fts: FtsConfig = field(default_factory=FtsConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)
    archival: ArchivalConfig = field(default_factory=ArchivalConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)

    # --- Derived paths (never serialized) -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def pinned_facts_path(self) -> Path:
        """Tier-0 flat file, always injected (I3)."""
        return self.data_dir / "pinned.md"

    @property
    def retrieval_log_path(self) -> Path:
        return self.data_dir / "retrieval.jsonl"

    @property
    def ledger_db_path(self) -> Path:
        """Outcome ledger (docs/PHASE6-LOOP.md) — its own file, backend-agnostic (L5)."""
        return self.data_dir / "ledger.db"

    @property
    def manifest_cache_path(self) -> Path:
        return self.data_dir / "manifest.cache.json"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def maintain_stamp_path(self) -> Path:
        """Timestamp of the last successful `maintain` run (surfaced by `status`)."""
        return self.data_dir / "maintain.last"

    @property
    def provenance_key_path(self) -> Path:
        """HMAC key for the provenance chain (T5). Losing it makes old chains unverifiable."""
        return self.data_dir / "key"

    @property
    def archive_db_path(self) -> Path:
        """Cold tier for decayed/superseded versions (PHASE9)."""
        return self.data_dir / "archive.db"


# --------------------------------------------------------------------------------------------
# Loading / merging
# --------------------------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _coerce(raw: Any, hint: Any) -> Any:
    """Coerce a raw value (possibly a string from env/TOML) to the field's declared type."""
    if hint is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}
    if hint is int:
        return int(raw)
    if hint is float:
        return float(raw)
    if hint is Path:
        return Path(str(raw)).expanduser()
    if hint is str:
        return str(raw)
    if get_origin(hint) is list:
        if isinstance(raw, list):
            return [str(item) for item in raw]
        text = str(raw).strip()
        if text.startswith("["):
            return [str(item) for item in json.loads(text)]
        return [part.strip() for part in text.split(",") if part.strip()]
    return raw


def _build(cls: Any, data: dict[str, Any]) -> Any:
    """Construct a (possibly nested) dataclass from a plain dict, coercing scalar types.

    Missing keys fall back to the dataclass's own defaults, which is how the defaults tier of
    the precedence chain is realized.
    """
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        hint = hints[f.name]
        raw = data[f.name]
        if is_dataclass(hint):
            kwargs[f.name] = _build(hint, raw if isinstance(raw, dict) else {})
        else:
            kwargs[f.name] = _coerce(raw, hint)
    return cls(**kwargs)


def _collect_env(instance: Any, prefix: list[str]) -> dict[str, Any]:
    """Walk the config schema and pull any matching ``SUP_MEM_*`` env vars into a dict."""
    out: dict[str, Any] = {}
    for f in fields(instance):
        value = getattr(instance, f.name)
        path = [*prefix, f.name]
        if is_dataclass(value) and not isinstance(value, type):
            sub = _collect_env(value, path)
            if sub:
                out[f.name] = sub
        else:
            env_key = ENV_PREFIX + "_".join(part.upper() for part in path)
            if env_key in os.environ:
                out[f.name] = os.environ[env_key]
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError):
        # A malformed config file must never crash the hot path; fall back to defaults.
        return {}


def create_private_file(path: Path) -> None:
    """Create ``path`` empty and owner-only (0600) if it does not exist yet.

    Data files hold memories and prompt text. Creating them 0600 up front (O_EXCL) leaves no
    world-readable window, and SQLite gives its -wal/-shm side files the db file's mode. An
    existing file is never touched — its mode is its owner's call. One syscall when present.
    """
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def _resolve_data_dir(overrides: dict[str, Any]) -> Path:
    if "data_dir" in overrides and overrides["data_dir"]:
        return Path(str(overrides["data_dir"])).expanduser()
    env = os.environ.get(ENV_PREFIX + "DATA_DIR")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DATA_DIR


def load_config(overrides: dict[str, Any] | None = None) -> Config:
    """Resolve the effective config: defaults ← config.toml ← env ← ``overrides`` (flags).

    ``config.toml`` is read from the resolved ``data_dir`` so that pointing ``data_dir`` at a
    scratch directory (e.g. in tests) yields clean defaults with no ambient-config leakage.
    """
    overrides = overrides or {}
    data_dir = _resolve_data_dir(overrides)

    file_dict = _read_toml(data_dir / "config.toml")
    env_dict = _collect_env(Config(), [])

    ledger_file = file_dict.get("ledger")
    if (
        isinstance(ledger_file, dict)
        and ledger_file.get("correction_patterns") == LEGACY_CORRECTION_PATTERNS
    ):
        del ledger_file["correction_patterns"]  # an untouched old default → today's default
    merged = _deep_merge(file_dict, env_dict)
    merged = _deep_merge(merged, overrides)
    merged["data_dir"] = str(data_dir)  # always the fully-resolved dir
    return cast(Config, _build(Config, merged))


def render_default_toml(config: Config | None = None) -> str:
    """Render a fully-documented ``config.toml`` with every §8 knob present (§13).

    ``data_dir`` is intentionally omitted — it locates this file, so it comes from env/flags.
    """
    c = config or Config()

    def _arr(values: list[str]) -> str:
        # Single-quoted TOML literals: no escaping needed for our regex patterns.
        return "[\n" + "".join(f"    '{v}',\n" for v in values) + "]"

    def _b(value: bool) -> str:
        return "true" if value else "false"

    return f"""# sup-mem configuration — https://github.com/kiraa06/sup-mem
# Precedence (low -> high): these defaults <- this file <- env (SUP_MEM_*) <- CLI flags.
# Env mirrors nested keys, e.g. SUP_MEM_RETRIEVAL_THRESHOLD, SUP_MEM_QDRANT_HNSW_M.

backend = "{c.backend}"   # "sqlite_fts" (default, no deps) | "qdrant" (vector) | "pgvector"

[retrieval]
k = {c.retrieval.k}              # max memories injected/returned per query
threshold = {c.retrieval.threshold}      # 0..1 relevance gate; tune with the retrieval log
max_inject_chars = {c.retrieval.max_inject_chars}   # per-memory hook clip; 0 = unlimited

[tier1]
# Skip-gate ONLY — never decides whether a relevant memory exists (I3). A turn is skipped iff
# a skip_pattern matches AND no cue_pattern matches. Single quotes = literal (no escaping).
skip_patterns = {_arr(c.tier1.skip_patterns)}
cue_patterns = {_arr(c.tier1.cue_patterns)}

[manifest]
full_below = {c.manifest.full_below}     # below this many memories: list topics verbatim
max_topics = {c.manifest.max_topics}
token_budget = {c.manifest.token_budget}
cache = {_b(c.manifest.cache)}

[chunking]
enabled = {_b(c.chunking.enabled)}
max_chars = {c.chunking.max_chars}
overlap = {c.chunking.overlap}

[fts]
k1 = {c.fts.k1}                 # FTS5 fixes k1/b internally; kept for completeness
b = {c.fts.b}
squash_midpoint = {c.fts.squash_midpoint}    # BM25 -> 0..1 logistic (the live FTS tuning knob)
squash_steepness = {c.fts.squash_steepness}

[embedding]
provider = "{c.embedding.provider}"   # "" = auto-detect on `setup`
model = "{c.embedding.model}"

[qdrant]
url = "{c.qdrant.url}"
collection = "{c.qdrant.collection}"
quantization = {_b(c.qdrant.quantization)}

[qdrant.hnsw]
m = {c.qdrant.hnsw.m}
ef_construct = {c.qdrant.hnsw.ef_construct}
ef = {c.qdrant.hnsw.ef}

[logging]
retrieval_log = {_b(c.logging.retrieval_log)}   # log (query, ids, scores, tier) for tuning (§8)

[ledger]
# The outcome loop (docs/PHASE6-LOOP.md): the Stop hook attributes each injected memory as
# referenced / ignored / contradicted; retrieval gets a bounded utility boost; `sup-mem tune`
# and `sup-mem roi` report on the evidence. Advisory + fail-open (L2/L3).
enabled = {_b(c.ledger.enabled)}
pool_k = {c.ledger.pool_k}              # candidates logged per turn for counterfactual tuning
boost_weight = {c.ledger.boost_weight}       # bounded score adjustment from outcomes
quarantine_contradictions = {c.ledger.quarantine_contradictions}
min_overlap_tokens = {c.ledger.min_overlap_tokens}
overlap_fraction = {c.ledger.overlap_fraction}
# Chronic-ignore demotion: injected >= min_injections times yet referenced at <= max_ref_rate
# → down-ranked by penalty (a demotion, not a drop — a strong match can still win).
chronic_ignore_min_injections = {c.ledger.chronic_ignore_min_injections}
chronic_ignore_max_ref_rate = {c.ledger.chronic_ignore_max_ref_rate}
chronic_ignore_penalty = {c.ledger.chronic_ignore_penalty}
correction_patterns = {_arr(c.ledger.correction_patterns)}

[maintenance]
# Housekeeping run by `sup-mem maintain`; schedule it with `sup-mem service install`.
log_keep_days = {c.maintenance.log_keep_days}
backup_keep = {c.maintenance.backup_keep}       # only distinct store states are backed up
candidates_keep = {c.maintenance.candidates_keep}  # cap the ledger candidates table (tune replay)
auto_tune = {_b(c.maintenance.auto_tune)}       # apply tune recommendation only when lossless
tune_min_attributed = {c.maintenance.tune_min_attributed}
notify = {_b(c.maintenance.notify)}
hour = {c.maintenance.hour}                # launchd schedule (daily)
minute = {c.maintenance.minute}

[provenance]
# Tamper-evident write chain (docs/PHASE8-TEMPORAL.md): every store/supersede/revive joins an
# HMAC hash chain keyed by ~/.sup-mem/key. Verify with `sup-mem verify` (also runs in maintain).
enabled = {_b(c.provenance.enabled)}

[archival]
# Evidence-based cold tier with size caps (docs/PHASE9-ARCHIVAL.md). Steady state archives
# superseded/quarantined versions; over main_max_mb the most-useless decayed memories move;
# over archive_max_mb the OLDEST ARCHIVED ARE DELETED FOREVER (FIFO, chain-audited).
# archive_max_mb = 0 disables permanent deletion entirely.
enabled = {_b(c.archival.enabled)}
main_max_mb = {c.archival.main_max_mb}
archive_max_mb = {c.archival.archive_max_mb}
superseded_after_days = {c.archival.superseded_after_days}
quarantined_after_days = {c.archival.quarantined_after_days}
decay_min_age_days = {c.archival.decay_min_age_days}
keep_tag = "{c.archival.keep_tag}"

[capture]
# PreCompact capture (docs/PHASE10-CAPTURE.md): just before Claude Code compacts a session,
# a headless `claude -p` call distills the transcript tail into durable memories so they
# survive compaction. COSTS TOKENS (one small-model call per compaction). Captured memories
# are tagged auto-capture and are judged by the outcome loop like everything else.
enabled = {_b(c.capture.enabled)}
model = "{c.capture.model}"
max_memories = {c.capture.max_memories}
max_transcript_chars = {c.capture.max_transcript_chars}
per_turn_chars = {c.capture.per_turn_chars}
min_transcript_chars = {c.capture.min_transcript_chars}  # below this, skip the model call
timeout_seconds = {c.capture.timeout_seconds}
"""


# --------------------------------------------------------------------------------------------
# In-place edits (tune --apply / maintain auto-tune)
# --------------------------------------------------------------------------------------------


class ConfigEditError(ValueError):
    """``config.toml`` could not be edited safely; it was left untouched."""


def _toml_scalar(value: float) -> str:
    # repr keeps a float a float ("1.0", never "1"); ints render bare.
    return repr(value) if isinstance(value, float) else str(value)


def set_retrieval_keys(path: Path, changes: dict[str, float]) -> None:
    """Set keys of the ``[retrieval]`` table IN PLACE — every other byte of the file survives.

    Re-rendering the whole file from the template (the old behaviour) dropped user comments
    and keys the template lacked, and persisted any active ``SUP_MEM_*`` env override. Here
    each changed key's value is rewritten on its own line (its inline comment kept); a key
    missing from ``[retrieval]`` is inserted under the header; a missing table is appended.
    A missing file starts from the pristine template, never from the env-merged config.

    Raises ``ConfigEditError`` — file untouched — when the existing file does not parse as
    TOML, or when the result would differ from the original in anything but ``changes``
    (an unusual layout, e.g. dotted ``retrieval.k`` keys). The write is atomic (temp file in
    the same dir + ``os.replace``) after copying the original to ``config.toml.bak``.
    """
    import copy
    import re
    import shutil
    import tempfile

    target = path.resolve()  # edit a symlink's target; never replace the link with a file
    if target.exists():
        try:
            original = target.read_text(encoding="utf-8")
            before = tomllib.loads(original)
        except (OSError, ValueError) as exc:  # TOMLDecodeError / UnicodeDecodeError
            raise ConfigEditError(
                f"{path} is not valid TOML ({exc}) — left untouched. Fix it: until then "
                "sup-mem ignores the file and runs on defaults."
            ) from exc
    else:
        original = render_default_toml()
        before = tomllib.loads(original)

    def _line(key: str, value: float) -> str:
        return f"{key} = {_toml_scalar(value)}\n"

    lines = original.splitlines(keepends=True)
    header = re.compile(r"^\s*\[(?P<aot>\[)?\s*(?P<name>[^\]]*?)\s*\]")
    heads = [i for i, line in enumerate(lines) if header.match(line)]
    section = None
    for i in heads:
        m = header.match(lines[i])
        if m and not m["aot"] and m["name"].strip("\"'") == "retrieval":
            section = i
            break
    if section is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["\n", "[retrieval]\n", *(_line(k, v) for k, v in changes.items())])
    else:
        end = next((i for i in heads if i > section), len(lines))
        for key, value in changes.items():
            pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)[^\s#]+(.*)$", re.DOTALL)
            for i in range(section + 1, end):
                if m := pattern.match(lines[i]):
                    lines[i] = f"{m[1]}{_toml_scalar(value)}{m[2]}"  # inline comment kept
                    break
            else:
                if not lines[section].endswith("\n"):
                    lines[section] += "\n"
                lines.insert(section + 1, _line(key, value))
                end += 1
    text = "".join(lines)

    # Prove the edit: the result must equal the original with exactly ``changes`` applied.
    expected = copy.deepcopy(before)
    after: Any = None
    with contextlib.suppress(AttributeError, ValueError):  # non-table retrieval / bad TOML
        expected.setdefault("retrieval", {}).update(changes)
        after = tomllib.loads(text)
    if after != expected:
        wanted = ", ".join(_line(k, v).strip() for k, v in changes.items())
        raise ConfigEditError(
            f"could not edit [retrieval] in {path} safely (unusual layout) — left untouched. "
            f"Set {wanted} under [retrieval] by hand."
        )

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        shutil.copy2(target, target.with_name(target.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if target.exists():
            shutil.copymode(target, tmp)  # keep the user's permissions on the new inode
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
