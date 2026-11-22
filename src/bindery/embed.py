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
from functools import lru_cache
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

    def __init__(self, *, local_files_only: bool = False) -> None:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        try:
            available = {m["model"]: m for m in TextEmbedding.list_supported_models()}
        except Exception:
            available = {}
        options = {"local_files_only": True} if local_files_only else {}
        for candidate in MULTILINGUAL_MODELS:
            if available and candidate not in available:
                continue
            try:
                self._model = TextEmbedding(model_name=candidate, **options)
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

    def __init__(self, *, local_files_only: bool = False) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        last: Exception | None = None
        options = {"local_files_only": True} if local_files_only else {}
        for candidate in MULTILINGUAL_MODELS:
            try:
                self._model = SentenceTransformer(candidate, **options)
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


@lru_cache(maxsize=2)
def load_backend(*, local_files_only: bool = False) -> EmbeddingBackend | None:
    """Return the first available backend, or ``None`` when none is installed.

    Cache by mode, including misses. A dashboard search only uses cached model
    files, and a cache miss must not prevent later explicit model setup.

    A missing backend is a normal, supported state rather than an error: the
    keyword index alone is a working system.
    """
    for factory in (_FastEmbedBackend, _SentenceTransformersBackend):
        try:
            return factory(local_files_only=local_files_only)
        except Exception:
            continue
    return None


def reset_backend() -> None:
    """Forget the cached backend. For tests."""
    load_backend.cache_clear()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
