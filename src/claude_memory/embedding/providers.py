"""Concrete embedding providers + side-effect-free availability probes (HANDOVER §6.4).

Each provider contributes:
  * ``available_<name>(config) -> (available, model)`` — a cheap probe (env check, ``find_spec``,
    or a short HTTP GET). It never downloads anything (§6.4).
  * ``build_<name>(config, model) -> Embedder`` — constructs the embedder (may load a model /
    open a client); called only AFTER a provider is chosen.
  * an entry in ``PROVIDER_SPECS`` (priority order) describing where it runs + hook-safety.

HTTP is done with stdlib ``urllib`` so the embedding path carries no extra dependency and can
run inside the warm MCP server without pulling an HTTP client.
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_memory.embedding.base import Embedder, EmbeddingMeta

if TYPE_CHECKING:
    from claude_memory.config import Config

_PROBE_TIMEOUT = 2.0
_EMBED_TIMEOUT = 60.0

# Known output dimensions so detection can show a table without constructing anything.
_KNOWN_DIMS: dict[str, int] = {
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
    "snowflake-arctic-embed": 1024,
    "BAAI/bge-small-en-v1.5": 384,
    "voyage-3": 1024,
    "text-embedding-3-small": 1536,
}

_OLLAMA_EMBED_MODELS = (
    "nomic-embed-text",
    "mxbai-embed-large",
    "all-minilm",
    "snowflake-arctic-embed",
)


def _http_get_json(url: str, timeout: float = _PROBE_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = _EMBED_TIMEOUT) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------------------------
# Ollama (local server)
# --------------------------------------------------------------------------------------------
def _ollama_host(config: Config) -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def available_ollama(config: Config) -> tuple[bool, str]:
    try:
        data = _http_get_json(f"{_ollama_host(config)}/api/tags")
    except Exception:
        return False, ""
    names = [str(m.get("name", "")).split(":")[0] for m in data.get("models", [])]
    for preferred in _OLLAMA_EMBED_MODELS:
        if any(n == preferred or n.startswith(preferred) for n in names):
            return True, preferred
    return False, ""


class OllamaEmbedder(Embedder):
    hook_safe = True

    def __init__(self, config: Config, model: str) -> None:
        self._host = _ollama_host(config)
        self._model = model
        self._dim = _KNOWN_DIMS.get(model, 0)

    @property
    def meta(self) -> EmbeddingMeta:
        if not self._dim:
            self._dim = len(self.embed(["probe"])[0])
        return EmbeddingMeta("ollama", self._model, self._dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            resp = _http_post_json(
                f"{self._host}/api/embeddings", {"model": self._model, "prompt": text}
            )
            vectors.append([float(x) for x in resp["embedding"]])
        return vectors


def build_ollama(config: Config, model: str) -> Embedder:
    return OllamaEmbedder(config, model)


# --------------------------------------------------------------------------------------------
# fastembed (in-process ONNX, CPU) — the default local embedder (§6.4)
# --------------------------------------------------------------------------------------------
def available_fastembed(config: Config) -> tuple[bool, str]:
    if importlib.util.find_spec("fastembed") is None:
        return False, ""
    return True, "BAAI/bge-small-en-v1.5"


class FastEmbedEmbedder(Embedder):
    hook_safe = False  # loads an ONNX model — reserved for the warm MCP server (I2)

    def __init__(self, config: Config, model: str) -> None:
        from fastembed import TextEmbedding

        self._model_name = model
        self._model = TextEmbedding(model_name=model)
        self._dim = 0

    @property
    def meta(self) -> EmbeddingMeta:
        if not self._dim:
            self._dim = len(self.embed(["probe"])[0])
        return EmbeddingMeta("fastembed", self._model_name, self._dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]


def build_fastembed(config: Config, model: str) -> Embedder:
    return FastEmbedEmbedder(config, model)


# --------------------------------------------------------------------------------------------
# TEI — Text Embeddings Inference server
# --------------------------------------------------------------------------------------------
def _tei_url() -> str:
    return os.environ.get("TEI_URL", "").rstrip("/")


def available_tei(config: Config) -> tuple[bool, str]:
    url = _tei_url()
    if not url:
        return False, ""
    try:
        _http_get_json(f"{url}/health")
    except Exception:
        return False, ""
    return True, os.environ.get("TEI_MODEL", "tei")


class TeiEmbedder(Embedder):
    hook_safe = True

    def __init__(self, config: Config, model: str) -> None:
        self._url = _tei_url()
        self._model = model or "tei"
        self._dim = 0

    @property
    def meta(self) -> EmbeddingMeta:
        if not self._dim:
            self._dim = len(self.embed(["probe"])[0])
        return EmbeddingMeta("tei", self._model, self._dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = _http_post_json(f"{self._url}/embed", {"inputs": list(texts)})
        return [[float(x) for x in vector] for vector in resp]


def build_tei(config: Config, model: str) -> Embedder:
    return TeiEmbedder(config, model)


# --------------------------------------------------------------------------------------------
# Hosted APIs — Voyage / OpenAI (network + cost; leaves your box)
# --------------------------------------------------------------------------------------------
def available_voyage(config: Config) -> tuple[bool, str]:
    if os.environ.get("VOYAGE_API_KEY") and importlib.util.find_spec("voyageai") is not None:
        return True, "voyage-3"
    return False, ""


class VoyageEmbedder(Embedder):
    hook_safe = True

    def __init__(self, config: Config, model: str) -> None:
        import voyageai

        self._client = voyageai.Client()
        self._model = model
        self._dim = _KNOWN_DIMS.get(model, 0)

    @property
    def meta(self) -> EmbeddingMeta:
        if not self._dim:
            self._dim = len(self.embed(["probe"])[0])
        return EmbeddingMeta("voyage", self._model, self._dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = self._client.embed(list(texts), model=self._model, input_type="document")
        return [[float(x) for x in vector] for vector in result.embeddings]


def build_voyage(config: Config, model: str) -> Embedder:
    return VoyageEmbedder(config, model)


def available_openai(config: Config) -> tuple[bool, str]:
    if os.environ.get("OPENAI_API_KEY") and importlib.util.find_spec("openai") is not None:
        return True, "text-embedding-3-small"
    return False, ""


class OpenAIEmbedder(Embedder):
    hook_safe = True

    def __init__(self, config: Config, model: str) -> None:
        from openai import OpenAI

        self._client = OpenAI()
        self._model = model
        self._dim = _KNOWN_DIMS.get(model, 0)

    @property
    def meta(self) -> EmbeddingMeta:
        if not self._dim:
            self._dim = len(self.embed(["probe"])[0])
        return EmbeddingMeta("openai", self._model, self._dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = self._client.embeddings.create(model=self._model, input=list(texts))
        return [[float(x) for x in item.embedding] for item in result.data]


def build_openai(config: Config, model: str) -> Embedder:
    return OpenAIEmbedder(config, model)


# --------------------------------------------------------------------------------------------
# Registry (priority order — see §6.4)
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderSpec:
    name: str
    where: str
    latency: str
    hook_safe: bool
    default_model: str
    default_dim: int | None
    probe_attr: str  # module-level (config) -> (available, model)
    build_attr: str  # module-level (config, model) -> Embedder


PROVIDER_SPECS: list[ProviderSpec] = [
    ProviderSpec(
        "ollama",
        "local server",
        "fast",
        True,
        "nomic-embed-text",
        768,
        "available_ollama",
        "build_ollama",
    ),
    ProviderSpec(
        "fastembed",
        "in-process",
        "fast",
        False,
        "BAAI/bge-small-en-v1.5",
        384,
        "available_fastembed",
        "build_fastembed",
    ),
    ProviderSpec("tei", "local server", "fast", True, "tei", None, "available_tei", "build_tei"),
    ProviderSpec(
        "voyage",
        "hosted API",
        "network",
        True,
        "voyage-3",
        1024,
        "available_voyage",
        "build_voyage",
    ),
    ProviderSpec(
        "openai",
        "hosted API",
        "network",
        True,
        "text-embedding-3-small",
        1536,
        "available_openai",
        "build_openai",
    ),
]

SPEC_BY_NAME: dict[str, ProviderSpec] = {spec.name: spec for spec in PROVIDER_SPECS}
