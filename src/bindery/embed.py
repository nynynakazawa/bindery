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


class _FastEmbedBackend:
    """`fastembed` - ONNX runtime, no torch, multilingual model available."""

    name = "fastembed"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]

        self._model = TextEmbedding(model_name="intfloat/multilingual-e5-small")
        self.dim = 384

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, vec)) for vec in self._model.embed(texts)]


class _SentenceTransformersBackend:
    """`sentence-transformers` - heavier, but common in existing environments."""

    name = "sentence-transformers"

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

        self._model = SentenceTransformer("intfloat/multilingual-e5-small")
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=False)
        return [list(map(float, vec)) for vec in vectors]


def load_backend() -> EmbeddingBackend | None:
    """Return the first available backend, or ``None`` when none is installed.

    A missing backend is a normal, supported state rather than an error: the
    keyword index alone is a working system.
    """
    for factory in (_FastEmbedBackend, _SentenceTransformersBackend):
        try:
            return factory()  # type: ignore[return-value]
        except Exception:
            continue
    return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
