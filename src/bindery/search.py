"""Hybrid retrieval under a hard token budget.

Two independent rankings are produced - BM25 over the trigram full-text index
and, when embeddings are available, cosine similarity over chunk vectors. They
are combined with Reciprocal Rank Fusion, which needs no score calibration
between the two and so cannot be thrown off by BM25 and cosine living on
different scales.

A third, learned signal is then applied: notes that have answered questions
before are nudged upward, and notes that stopped being useful decay back down.
The nudge is bounded (see ``growth.USAGE_BOOST_WEIGHT``) so that history can
break ties but can never outrank an actual content match.

Searching is therefore a write operation - each call records what was asked and
what answered it, which is the signal the memory grows on. Pass ``learn=False``
for read-only searches that should not train the ranking.

The budget is applied last and is a hard cap. A caller asking for eight results
gets fewer than eight if eight would not fit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .embed import cosine, load_backend
from .growth import apply_usage_boost
from .store import Chunk, Store, unpack_vector
from .tokens import estimate_tokens

#: RRF damping constant. 60 is the value from the original formulation and is
#: not sensitive enough to be worth exposing as a setting.
RRF_K = 60

#: Characters FTS5 treats as query syntax rather than content.
_FTS_SPECIAL = re.compile(r'["*(){}:^-]')


@dataclass(slots=True)
class Hit:
    chunk: Chunk
    score: float
    matched_by: str


#: FTS5's trigram tokenizer indexes overlapping 3-character sequences, so it
#: structurally cannot match a query shorter than this. Japanese is full of
#: two-character words - 認証, 設計, 課金 - so those queries need another path.
TRIGRAM_MIN = 3


def _split_terms(raw: str) -> tuple[list[str], list[str]]:
    """Split a query into ``(trigram_terms, short_terms)``."""
    cleaned = _FTS_SPECIAL.sub(" ", raw).strip()
    if not cleaned:
        return [], []
    parts = cleaned.split()
    long_terms = [t for t in parts if len(t) >= TRIGRAM_MIN]
    short_terms = [t for t in parts if 0 < len(t) < TRIGRAM_MIN]
    if not long_terms and not short_terms:
        return [], []
    return long_terms, short_terms


def _fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every term is quoted, which both neutralises FTS5 operators and is required
    for the trigram tokenizer to handle Japanese input containing no spaces.
    """
    long_terms, _ = _split_terms(raw)
    return " OR ".join(f'"{t}"' for t in long_terms)


def _like_ranking(store: Store, terms: list[str], depth: int) -> list[int]:
    """Substring fallback for terms too short for the trigram index.

    This is a table scan, which is acceptable at the scale of a personal note
    collection and is strictly better than returning nothing. Results are
    ordered by chunk size so that a short, focused passage outranks a long one
    that merely happens to contain the term.
    """
    if not terms:
        return []
    body_clauses = ["body LIKE ? ESCAPE '\\'" for _ in terms]
    heading_clauses = ["heading LIKE ? ESCAPE '\\'" for _ in terms]
    where = " OR ".join(body_clauses + heading_clauses)
    params: list[str] = []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    params = params + list(params)
    try:
        rows = store.conn.execute(
            f"SELECT id FROM chunks WHERE {where} ORDER BY tokens ASC LIMIT ?",
            [*params, depth],
        ).fetchall()
    except Exception:
        return []
    return [int(r["id"]) for r in rows]


def _keyword_ranking(store: Store, query: str, depth: int) -> list[int]:
    long_terms, short_terms = _split_terms(query)
    ranked: list[int] = []

    expression = " OR ".join(f'"{t}"' for t in long_terms)
    if expression:
        try:
            rows = store.conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts, 2.0, 1.0) LIMIT ?",
                (expression, depth),
            ).fetchall()
            ranked.extend(int(r["rowid"]) for r in rows)
        except Exception:
            pass

    if short_terms:
        for chunk_id in _like_ranking(store, short_terms, depth):
            if chunk_id not in ranked:
                ranked.append(chunk_id)

    return ranked[:depth]


def _semantic_ranking(store: Store, query: str, depth: int) -> list[int]:
    backend = load_backend()
    if backend is None:
        return []
    rows = store.all_vectors()
    if not rows:
        return []
    try:
        query_vec = backend.encode([query])[0]
    except Exception:
        return []
    scored = [
        (int(r["chunk_id"]), cosine(query_vec, unpack_vector(r["vec"], int(r["dim"]))))
        for r in rows
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [chunk_id for chunk_id, _ in scored[:depth]]


def _fuse(rankings: dict[str, list[int]]) -> dict[int, tuple[float, list[str]]]:
    fused: dict[int, tuple[float, list[str]]] = {}
    for label, ranking in rankings.items():
        for rank, chunk_id in enumerate(ranking):
            score, sources = fused.get(chunk_id, (0.0, []))
            fused[chunk_id] = (score + 1.0 / (RRF_K + rank + 1), [*sources, label])
    return fused


def search(
    config: Config,
    store: Store,
    query: str,
    *,
    limit: int | None = None,
    max_tokens: int | None = None,
    learn: bool = True,
) -> tuple[list[Hit], dict[str, int]]:
    """Return fused hits that fit inside the token budget."""
    limit = limit or config.limit
    budget = max_tokens or config.max_tokens
    depth = max(limit * 4, 20)

    rankings = {"keyword": _keyword_ranking(store, query, depth)}
    if config.semantic:
        semantic = _semantic_ranking(store, query, depth)
        if semantic:
            rankings["semantic"] = semantic

    fused = _fuse(rankings)
    if not fused:
        # A miss is recorded too. Repeated misses are the clearest statement
        # the system ever gets about what it is missing.
        if learn:
            store.record_query(query, 0)
            store.commit()
        return [], {"returned": 0, "tokens": 0, "considered": 0, "truncated": 0}

    flat = [(chunk_id, score, sources) for chunk_id, (score, sources) in fused.items()]
    flat.sort(key=lambda item: item[1], reverse=True)
    chunks = store.chunk_rows([chunk_id for chunk_id, _, _ in flat])
    ordered = [
        (chunk_id, (score, sources))
        for chunk_id, score, sources in apply_usage_boost(
            flat,
            {chunk_id: chunk.path for chunk_id, chunk in chunks.items()},
            store.usage_map(),
        )
    ]

    hits: list[Hit] = []
    spent = 0
    truncated = 0
    for chunk_id, (score, sources) in ordered:
        if len(hits) >= limit:
            break
        chunk = chunks.get(chunk_id)
        if chunk is None:
            continue
        cost = chunk.tokens or estimate_tokens(chunk.body)
        if spent + cost > budget:
            # Budget exhausted. Keep scanning only to count what was dropped,
            # so the caller can tell "nothing matched" from "too much matched".
            truncated += 1
            continue
        hits.append(Hit(chunk=chunk, score=score, matched_by="+".join(sorted(set(sources)))))
        spent += cost

    if learn:
        store.record_query(query, len(hits))
        store.record_use(sorted({hit.chunk.path for hit in hits}))
        store.commit()

    return hits, {
        "returned": len(hits),
        "tokens": spent,
        "considered": len(ordered),
        "truncated": truncated,
    }
