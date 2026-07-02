"""Configuration for sup-mem.

Precedence (low → high): dataclass defaults ← ``config.toml`` ← environment ← explicit flags.

Every tunable from HANDOVER §8 lives here with a sane default; nothing operational is
hard-coded in logic (I9). Env vars mirror the nested keys, upper-cased and joined with ``_``
under the ``SUP_MEM_`` prefix, e.g. ``SUP_MEM_RETRIEVAL_THRESHOLD`` and
``SUP_MEM_QDRANT_HNSW_M`` (§13).

This module is stdlib-only so it is safe to import on the hook's hot path (I2).
"""

from __future__ import annotations

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
    token_budget: int = 600  # approximate cap on the injected manifest
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


@dataclass
class LedgerConfig:
    """The outcome loop (docs/PHASE6-LOOP.md): attribution, reinforcement, quarantine."""

    enabled: bool = True
    pool_k: int = 12  # candidates logged per turn (>= retrieval.k) for counterfactual tuning
    boost_weight: float = 0.10  # bounded utility boost; base relevance always dominates (L3)
    quarantine_contradictions: int = 3  # contradictions needed (and > references) to drop
    min_overlap_tokens: int = 3  # distinctive-token matches for a "referenced" verdict
    overlap_fraction: float = 0.15  # ... or this fraction of the memory's tokens, if larger
    # A correction in the user turn right after a referenced memory flips it to contradicted.
    # NOTE: rendered as single-quoted TOML literals — keep apostrophes out of these patterns.
    correction_patterns: list[str] = field(
        default_factory=lambda: [
            r"^\s*no[,.! ]",
            r"\bthat.?s (wrong|incorrect|outdated|stale|not right)\b",
            r"\bnot (true|correct|right)\b",
            r"\bactually[, ]",
            r"\b(wrong|incorrect|outdated)\b.{0,20}\bmemory\b",
        ]
    )


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
    backup_keep: int = 7  # timestamped store backups retained
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
correction_patterns = {_arr(c.ledger.correction_patterns)}

[maintenance]
# Housekeeping run by `sup-mem maintain`; schedule it with `sup-mem service install`.
log_keep_days = {c.maintenance.log_keep_days}
backup_keep = {c.maintenance.backup_keep}
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
"""
