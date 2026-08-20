"""Optional local embeddings.

Semantic search is a genuine improvement over keyword matching, but the models
that provide it are large. Making embeddings optional keeps the base install at
zero dependencies, which matters because this server has to start reliably
inside two different agent runtimes.

Every backend here is local. Nothing in this module calls a paid API - sending
each query to a hosted embedding service would reintroduce exactly the running
token cost the project exists to remove.
"""

from __future__ import annotations

import math
from typing import Protocol


class EmbeddingBackend(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


#: Multilingual models, smallest first. A list rather than one name because
#: pinning a single one is how semantic search quietly stopped working:
#: `intfloat/multilingual-e5-small` was hard-coded here, fastembed dropped it
#: from its registry, every construction raised, and the exception was caught
#: and read as "no backend installed". Installing the extra did nothing and
#: said nothing.
#:
#: Multilingual is the requirement, not a preference - an English-only model
#: cannot embed the Japanese half of a bilingual vault.
MULTILINGUAL_MODELS = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "intfloat/multilingual-e5-small",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
    "intfloat/multilingual-e5-large",
)


class _FastEmbedBackend:
    """`fastembed` - ONNX runtime, no torch, multilingual model available."""

    name = "fastembed"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        try:
            available = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        except Exception:
            available = {}
        for candidate in MULTILINGUAL_MODELS:
            if available and candidate not in available:
                continue
            try:
                self._model = TextEmbedding(model_name=candidate)
            except Exception:
                continue
            self.model_name = candidate
            entry = available.get(candidate) or {}
            self.dim = int(entry.get("dim") or len(self.encode(["dimension probe"])[0]))
            return
        raise RuntimeError("fastembed has no supported multilingual model")

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, vec)) for vec in self._model.embed(texts)]


class _SentenceTransformersBackend:
    """`sentence-transformers` - heavier, but common in existing environments."""

    name = "sentence-transformers"

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        last: Exception | None = None
        for candidate in MULTILINGUAL_MODELS:
            try:
                self._model = SentenceTransformer(candidate)
            except Exception as exc:
                last = exc
                continue
            self.model_name = candidate
            self.dim = int(self._model.get_sentence_embedding_dimension())
            return
        raise RuntimeError(f"no multilingual model could be loaded: {last}")

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=False)
        return [list(map(float, vec)) for vec in vectors]


#: Resolved once per process. ``False`` records "looked, found nothing", which
#: is different from "not looked yet" - without that distinction every search
#: on a machine with no backend installed would retry two imports.
_BACKEND: "EmbeddingBackend | None | bool" = False


def load_backend() -> EmbeddingBackend | None:
    """Return the first available backend, or ``None`` when none is installed.

    Cached, because constructing a backend loads an ONNX model from disk - a
    matter of seconds. This is called on every semantic search and after every
    write, so paying that once per process rather than once per call is the
    difference between semantic search being usable and being unusable.

    A missing backend is a normal, supported state rather than an error: the
    keyword index alone is a working system.
    """
    global _BACKEND
    if _BACKEND is not False:
        return _BACKEND  # type: ignore[return-value]
    for factory in (_FastEmbedBackend, _SentenceTransformersBackend):
        try:
            _BACKEND = factory()  # type: ignore[assignment]
            return _BACKEND  # type: ignore[return-value]
        except Exception:
            continue
    _BACKEND = None
    return None


def reset_backend() -> None:
    """Forget the cached backend. For tests."""
    global _BACKEND
    _BACKEND = False


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
