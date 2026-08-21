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
from .embed import cosine
from .growth import apply_tier_prior, apply_usage_boost, shingles
from .store import Chunk, Store, unpack_vector
from .tokens import estimate_tokens

#: How similar two passages have to be before the second one is dropped from
#: the results. Lower than the threshold used for reporting duplicate *notes*,
#: because here the cost of keeping a redundant passage is immediate - it is
#: budget spent restating something the caller has already been told.
RESULT_DUPLICATE_THRESHOLD = 0.7


def _is_redundant(signature: set[int], kept: list[set[int]]) -> bool:
    if not signature:
        return False
    for other in kept:
        if not other:
            continue
        overlap = len(signature & other)
        if overlap / min(len(signature), len(other)) >= RESULT_DUPLICATE_THRESHOLD:
            return True
    return False


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

#: Scripts written without spaces between words. A query in these is one
#: "term" as far as whitespace splitting is concerned, however many ideas it
#: contains.
_CJK_RANGES = (
    (0x3040, 0x30FF),   # hiragana, katakana
    (0x3400, 0x4DBF),   # CJK extension A
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0xF900, 0xFAFF),   # compatibility ideographs
)

#: A CJK term longer than this is treated as a phrase to be broken up rather
#: than a word to be matched whole.
CJK_PHRASE_MIN = 5

#: Window size for that decomposition. Four characters is long enough to carry
#: meaning in Japanese - 認証方式, 実行時依存 - and short enough that a term the
#: note phrases slightly differently still overlaps somewhere.
CJK_WINDOW = 4


def _is_cjk(text: str) -> bool:
    return any(
        any(low <= ord(ch) <= high for low, high in _CJK_RANGES)
        for ch in text
    )


def _windows(term: str) -> list[str]:
    """Overlapping slices of a space-free CJK phrase, longest first.

    Without a morphological analyser there is no way to know where the words
    are, and the trigram index can only match a query as one contiguous
    substring. So `依存を増やさない理由` matched nothing while
    `依存を増やさない` matched the answer: one extra word, and a phrase that is
    present in the note stops being findable.

    Sliding a window over it gives back the substrings that *are* present.
    They are ORed, so BM25 weighs how many of them a passage contains, and a
    common window like `について` carries almost no weight because it appears
    everywhere.
    """
    if len(term) < CJK_PHRASE_MIN or not _is_cjk(term):
        return [term]
    slices = [term]
    for start in range(0, len(term) - CJK_WINDOW + 1, 2):
        slices.append(term[start : start + CJK_WINDOW])
    tail = term[-CJK_WINDOW:]
    if tail not in slices:
        slices.append(tail)
    return slices


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


def _like_ranking(
    store: Store, terms: list[str], depth: int, scope_sql: str = "", scope_params: list | None = None
) -> list[int]:
    """Substring fallback for terms too short for the trigram index.

    This is a table scan, which is acceptable at the scale of a personal note
    collection and is strictly better than returning nothing.

    Ranking used to be chunk length alone, which is only a tiebreaker dressed
    up as a ranking: for 認証 it put a two-line note reading "認証済み。" above
    the actual design document, because the note was shorter. How many of the
    query's terms a passage contains comes first now, then where they appear -
    title and breadcrumb beat body - and length only separates what is left.
    """
    if not terms:
        return []
    escaped = [
        f"%{term.replace(chr(92), chr(92) * 2).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%"
        for term in terms
    ]
    # One point per term found anywhere, plus a bonus for a strong field.
    score_parts = []
    params: list[str] = []
    for pattern in escaped:
        score_parts.append(
            "(CASE WHEN chunks.body LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) + "
            "(CASE WHEN chunks.breadcrumb LIKE ? ESCAPE '\\' THEN 2 ELSE 0 END) + "
            "(CASE WHEN notes.title LIKE ? ESCAPE '\\' THEN 3 ELSE 0 END)"
        )
        params.extend([pattern, pattern, pattern])
    score = " + ".join(score_parts)
    try:
        rows = store.conn.execute(
            f"""SELECT chunks.id, ({score}) AS score FROM chunks
                JOIN notes ON notes.id = chunks.note_id
                WHERE score > 0 {scope_sql}
                ORDER BY score DESC, chunks.tokens ASC LIMIT ?""",
            [*params, *(scope_params or []), depth],
        ).fetchall()
    except Exception:
        return []
    return [int(r["id"]) for r in rows]


#: BM25 column weights, in schema order: title, tags, breadcrumb, body.
#: A hit in the note's title is a much stronger statement about what the note
#: is about than the same word buried in a paragraph.
_BM25_WEIGHTS = (4.0, 3.0, 2.0, 1.0)


def _fts_match(
    store: Store, expression: str, depth: int, scope_sql: str, scope_params: list | None
) -> list[int]:
    if not expression:
        return []
    weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
    try:
        rows = store.conn.execute(
            f"""SELECT chunks_fts.rowid AS rowid FROM chunks_fts
                JOIN chunks ON chunks.id = chunks_fts.rowid
                JOIN notes ON notes.id = chunks.note_id
                WHERE chunks_fts MATCH ? {scope_sql}
                ORDER BY bm25(chunks_fts, {weights}) LIMIT ?""",
            (expression, *(scope_params or []), depth),
        ).fetchall()
    except Exception:
        return []
    return [int(r["rowid"]) for r in rows]


def _keyword_rankings(
    store: Store, query: str, depth: int, scope_sql: str = "", scope_params: list | None = None
) -> dict[str, list[int]]:
    """Two keyword rankings, strict and loose, to be fused rather than chosen.

    OR alone was too blunt: `認証 設計 Firebase` matched any passage mentioning
    Firebase and nothing else, and recall bought with that much noise is not
    worth having. AND alone is too brittle - one term the note happens not to
    use and a perfect match disappears, which is how `依存を増やさない理由`
    found nothing while `依存を増やさない` found the answer.

    Producing both and letting the fusion combine them keeps the property that
    matters: passages containing every term outrank passages containing some,
    without passages containing some being dropped.
    """
    long_terms, short_terms = _split_terms(query)
    rankings: dict[str, list[int]] = {}

    quoted = [f'"{t}"' for t in long_terms]
    if quoted:
        strict = _fts_match(store, " AND ".join(quoted), depth, scope_sql, scope_params)
        if strict and len(quoted) > 1:
            rankings["all-terms"] = strict
        loose = _fts_match(store, " OR ".join(quoted), depth, scope_sql, scope_params)
        if loose:
            rankings["keyword"] = loose
        if not strict:
            # Nothing matched the phrases whole. Fall back to their parts,
            # which is the only way a space-free CJK query survives being one
            # word longer than the note it is looking for.
            pieces = [f'"{w}"' for term in long_terms for w in _windows(term)]
            if len(pieces) > len(quoted):
                partial = _fts_match(
                    store, " OR ".join(dict.fromkeys(pieces)), depth, scope_sql, scope_params
                )
                if partial:
                    rankings["partial"] = partial

    if short_terms:
        substring = _like_ranking(store, short_terms, depth, scope_sql, scope_params)
        if substring:
            rankings["substring"] = substring

    return rankings


#: How far past the requested depth to look when the ANN index does the
#: search. Nearest-neighbour queries cannot express "and only this project",
#: so the filter is applied afterwards and the over-fetch is what stops a
#: scoped search from coming back short.
ANN_OVERFETCH = 8


def _semantic_ranking(
    store: Store, query: str, depth: int, scope_sql: str = "", scope_params: list | None = None
) -> list[int]:
    # Imported here rather than at module scope so that a caller replacing
    # `bindery.embed.load_backend` replaces it everywhere - it is the seam
    # tests use, and one module holding its own reference silently opted out.
    from .embed import load_backend

    backend = load_backend()
    if backend is None:
        return []
    try:
        query_vec = backend.encode([query])[0]
    except Exception:
        return []

    approximate = store.nearest_vectors(query_vec, depth * ANN_OVERFETCH)
    if approximate is not None:
        return store.filter_chunks_in_scope(approximate, scope_sql, scope_params)[:depth]

    # No ANN index: compare against every vector. Correct, and linear in the
    # size of the vault - fine for a personal collection, and the reason the
    # index above is worth having when it is available.
    rows = store.all_vectors(scope_sql, scope_params)
    if not rows:
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
    scope: str = "project",
) -> tuple[list[Hit], dict[str, int]]:
    """Return fused hits that fit inside the token budget.

    ``scope`` decides whose memory is searched. Defaulting to the current
    project matters more than it looks: the agent instructions this project
    installs say that a past decision overrides a fresh guess, so a decision
    retrieved from an unrelated repository is not merely noise - it is
    actively misleading, and phrased with all the authority of a real one.
    """
    limit = limit or config.limit
    budget = max_tokens or config.max_tokens
    depth = max(limit * 4, 20)
    scope_sql, scope_params = store.scope_clause(scope, config.project)

    rankings = _keyword_rankings(store, query, depth, scope_sql, scope_params)
    if config.semantic:
        semantic = _semantic_ranking(store, query, depth, scope_sql, scope_params)
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
    tiered = apply_tier_prior(flat, {cid: c.tier for cid, c in chunks.items()})
    ordered = [
        (chunk_id, (score, sources))
        for chunk_id, score, sources in apply_usage_boost(
            tiered,
            {chunk_id: chunk.path for chunk_id, chunk in chunks.items()},
            store.usage_map(),
        )
    ]

    hits: list[Hit] = []
    spent = 0
    truncated = 0
    redundant = 0
    kept_shingles: list[set[int]] = []
    for chunk_id, (score, sources) in ordered:
        if len(hits) >= limit:
            break
        chunk = chunks.get(chunk_id)
        if chunk is None:
            continue
        signature = shingles(chunk.body)
        if _is_redundant(signature, kept_shingles):
            # The same fact usually exists in several places at once - a
            # durable note, the journal entry it came from, and the episode
            # underneath that. Returning all of them spends the budget saying
            # one thing, when the budget is the only thing making this
            # affordable. Near-duplicates are dropped in favour of the next
            # distinct passage.
            redundant += 1
            continue
        cost = chunk.tokens or estimate_tokens(chunk.body)
        if spent + cost > budget:
            # Budget exhausted. Keep scanning only to count what was dropped,
            # so the caller can tell "nothing matched" from "too much matched".
            truncated += 1
            continue
        hits.append(Hit(chunk=chunk, score=score, matched_by="+".join(sorted(set(sources)))))
        kept_shingles.append(signature)
        spent += cost

    if learn:
        store.record_query(query, len(hits))
        # Shown, not used. What raises a note's ranking is an agent going on to
        # read it (see Store.record_use) - being returned is a statement about
        # the current ranking, and feeding it back in only confirms itself.
        store.record_impressions(sorted({hit.chunk.path for hit in hits}))
        store.commit()

    return hits, {
        "returned": len(hits),
        "tokens": spent,
        "considered": len(ordered),
        "truncated": truncated,
        "redundant": redundant,
    }


def count_outside_scope(config: Config, store: Store, query: str) -> int:
    """How many passages a wider scope would have reached.

    Without this a scoped search that finds nothing is indistinguishable from
    an empty vault, and the agent has no reason to look further - the memory
    would appear not to have the answer when it plainly does.
    """
    if not config.project:
        return 0
    depth = 200
    def flatten(scope: str) -> set[int]:
        sql, params = store.scope_clause(scope, config.project)
        found: set[int] = set()
        for ranking in _keyword_rankings(store, query, depth, sql, params).values():
            found.update(ranking)
        return found

    return len(flatten("all") - flatten("project"))
