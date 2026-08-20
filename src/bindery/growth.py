"""The growth loop.

A memory that only stores is a filing cabinet. This module is what makes the
store improve on its own, and it is built on one principle: **the retrievals
themselves are the training signal.** Every search says what an agent wanted;
every returned passage says what answered it. That signal is free, it needs no
model call, and it accumulates whether or not anyone remembers to curate.

The work is split by how safe it is to automate:

  automatic, always on      - usage-weighted ranking, recency decay, gap logging
  detected but not applied  - near-duplicates, stale notes, promotion candidates

Merging or deleting notes without a human or an agent deciding is how a memory
layer destroys the thing it was meant to protect, so consolidation stops at
detection and reports what it found.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .store import Store

#: Half-life for recency decay. A note retrieved 30 days ago carries half the
#: weight of the same note retrieved today.
USAGE_HALF_LIFE_DAYS = 30.0

#: Ceiling on how much learned usage may reshape a ranking. Deliberately small:
#: usage is evidence about importance, never about whether a passage answers
#: *this* query, so it may break ties but must not override a content match.
USAGE_BOOST_WEIGHT = 0.25

#: A pinned note is one a human marked as durable. It gets the full boost
#: regardless of how often it has been read.
PINNED_SCORE = 1.0

#: Near-duplicate detection.
SHINGLE_SIZE = 5
SIGNATURE_SIZE = 32
DUPLICATE_THRESHOLD = 0.8

#: A signature value shared by more passages than this is too common to be
#: discriminating, so it is skipped rather than generating a burst of useless
#: candidate pairs.
MAX_BUCKET = 40

#: Passages shorter than this are skipped by duplicate detection. A single
#: short sentence is contained in plenty of longer notes without that meaning
#: anything, and flagging those would bury the real duplicates.
MIN_SHINGLES_FOR_DUPLICATE = 10

#: A note is "stale" once it has gone this long without ever being retrieved.
STALE_AFTER_DAYS = 90.0

#: How many journal entries must share a tag before it is worth promoting into
#: a durable note of its own.
PROMOTION_MIN_ENTRIES = 3

#: --- automatic session capture -------------------------------------------
#: An MCP server cannot make an agent do anything - it only answers calls. So
#: the one thing it *can* capture without being asked is its own traffic: what
#: was searched, what came back empty, what was written. That is a record of
#: activity, not of insight, which is why it is kept separate from the entries
#: an agent writes deliberately through ``memory_learn``.

#: Directory for machine-written session records, kept apart from the agent's
#: own journal so that automatic noise never dilutes deliberate notes.
SESSION_PREFIX = "journal/sessions/"

#: A session must carry at least this many signals before it is worth writing
#: down. A "signal" is an unanswered question or a write - never a plain
#: successful search, because a session that only found what it needed taught
#: the system nothing new. This is the threshold that decides whether capture
#: happens at all.
AUTO_CAPTURE_MIN_SIGNALS = 2

#: Cap on how many unanswered questions one session record may list, so a
#: pathological session cannot write a huge note.
AUTO_CAPTURE_MAX_QUESTIONS = 12


# --------------------------------------------------------------- ranking --


def usage_score(path: str, usage: dict[str, tuple[int, float, int]], now: float) -> float:
    """Return a value in ``[0, 1]`` describing how load-bearing a note is.

    Frequency is compressed logarithmically so that one heavily-read note
    cannot dominate, and multiplied by an exponential recency decay so that
    knowledge which stopped being useful quietly stops being boosted.
    """
    entry = usage.get(path)
    if entry is None:
        return 0.0
    uses, last_used, pinned = entry
    if pinned:
        return PINNED_SCORE
    if uses <= 0 or last_used <= 0:
        return 0.0
    age_days = max(0.0, (now - last_used) / 86400.0)
    recency = 0.5 ** (age_days / USAGE_HALF_LIFE_DAYS)
    # log1p(uses) / log1p(20) saturates near 1.0 at about twenty retrievals.
    frequency = min(1.0, math.log1p(uses) / math.log1p(20))
    return frequency * recency


def apply_usage_boost(
    scored: list[tuple[int, float, list[str]]],
    chunk_paths: dict[int, str],
    usage: dict[str, tuple[int, float, int]],
    now: float | None = None,
) -> list[tuple[int, float, list[str]]]:
    """Re-weight fused results by what has proven useful before."""
    if not usage:
        return scored
    stamp = now if now is not None else time.time()
    boosted = [
        (
            chunk_id,
            score * (1.0 + USAGE_BOOST_WEIGHT * usage_score(chunk_paths.get(chunk_id, ""), usage, stamp)),
            sources,
        )
        for chunk_id, score, sources in scored
    ]
    boosted.sort(key=lambda item: item[1], reverse=True)
    return boosted


# ------------------------------------------------------------------ gaps --


@dataclass(slots=True)
class Gap:
    query: str
    count: int
    last_seen: float


def knowledge_gaps(store: Store, *, limit: int = 15, min_count: int = 2) -> list[Gap]:
    """Queries the agents kept asking that the memory could not answer.

    This is the most actionable output of the whole module: it is a list, in
    the agents' own words, of what the knowledge base is missing.
    """
    rows = store.conn.execute(
        "SELECT text, COUNT(*) AS n, MAX(ts) AS last FROM queries "
        "WHERE hits = 0 GROUP BY LOWER(TRIM(text)) HAVING n >= ? "
        "ORDER BY n DESC, last DESC LIMIT ?",
        (min_count, limit),
    ).fetchall()
    return [Gap(query=r["text"], count=int(r["n"]), last_seen=float(r["last"])) for r in rows]


def hot_paths(store: Store, *, limit: int = 10) -> list[tuple[str, int, float]]:
    """Notes that keep answering questions - the memory's load-bearing walls."""
    now = time.time()
    usage = store.usage_map()
    ranked = sorted(
        ((path, entry[0], usage_score(path, usage, now)) for path, entry in usage.items()),
        key=lambda row: row[2],
        reverse=True,
    )
    return [row for row in ranked if row[1] > 0][:limit]


# ------------------------------------------------------------ duplicates --


_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _shingles(text: str) -> set[int]:
    normalised = _normalise(text)
    if len(normalised) < SHINGLE_SIZE:
        return set()
    return {
        int.from_bytes(
            hashlib.blake2b(normalised[i : i + SHINGLE_SIZE].encode("utf-8"), digest_size=8).digest(),
            "little",
        )
        for i in range(len(normalised) - SHINGLE_SIZE + 1)
    }


def _signature(shingles: set[int]) -> frozenset[int]:
    """Bottom-k sketch: the k smallest shingle hashes.

    Returned as a set, not a sequence, because bottom-k sketches are compared
    by intersection rather than position. Slicing such a sketch into positional
    bands - the standard trick for a classic min-hash signature built from k
    independent hash functions - silently fails here: adding a sentence to a
    passage introduces new low hashes, every later value shifts along by one,
    and two passages with perfect overlap end up sharing no band at all.
    """
    return frozenset(sorted(shingles)[:SIGNATURE_SIZE])


def _containment(a: set[int], b: set[int]) -> float:
    """Overlap relative to the *smaller* passage.

    Jaccard is the obvious choice and the wrong one here. The redundancy that
    actually matters is "this has already been written down somewhere else",
    which usually looks like one passage being another plus a sentence or two.
    Jaccard punishes that asymmetry - a note that is a strict superset of
    another scores well below 1.0 and slips under any sensible threshold -
    whereas containment reports it as the near-total overlap it is.
    """
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if intersection == 0:
        return 0.0
    return intersection / min(len(a), len(b))


@dataclass(slots=True)
class DuplicatePair:
    left: str
    right: str
    similarity: float
    left_heading: str = ""
    right_heading: str = ""


def find_duplicates(store: Store, *, threshold: float = DUPLICATE_THRESHOLD) -> list[DuplicatePair]:
    """Find near-identical passages across different notes.

    Comparing every passage against every other is quadratic and becomes
    unusable on a real vault, so candidates are first bucketed by bands of
    their min-hash signature. Only passages that collide in a band are compared
    exactly.
    """
    rows = store.conn.execute(
        "SELECT c.id, c.breadcrumb, c.body, n.path FROM chunks c JOIN notes n ON n.id = c.note_id"
    ).fetchall()

    shingles: dict[int, set[int]] = {}
    buckets: dict[int, list[int]] = defaultdict(list)
    meta: dict[int, tuple[str, str]] = {}

    for row in rows:
        chunk_id = int(row["id"])
        sketch = _shingles(row["body"])
        if len(sketch) < MIN_SHINGLES_FOR_DUPLICATE:
            continue
        shingles[chunk_id] = sketch
        meta[chunk_id] = (row["path"], row["breadcrumb"])
        # Inverted index over sketch values: two passages that overlap heavily
        # necessarily share sketch values, so any shared value makes them
        # candidates and exact containment decides.
        for value in _signature(sketch):
            buckets[value].append(chunk_id)

    seen: set[tuple[int, int]] = set()
    pairs: list[DuplicatePair] = []
    for candidates in buckets.values():
        if len(candidates) < 2 or len(candidates) > MAX_BUCKET:
            continue
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                left, right = sorted((candidates[i], candidates[j]))
                if (left, right) in seen:
                    continue
                seen.add((left, right))
                # Two passages inside one note are expected - a note may repeat
                # itself across sections without that being a problem to fix.
                if meta[left][0] == meta[right][0]:
                    continue
                similarity = _containment(shingles[left], shingles[right])
                if similarity >= threshold:
                    pairs.append(
                        DuplicatePair(
                            left=meta[left][0],
                            right=meta[right][0],
                            similarity=round(similarity, 3),
                            left_heading=meta[left][1],
                            right_heading=meta[right][1],
                        )
                    )
    pairs.sort(key=lambda pair: pair.similarity, reverse=True)
    return pairs


# ----------------------------------------------------------------- stale --


def stale_notes(store: Store, *, limit: int = 15) -> list[tuple[str, float]]:
    """Notes that have never earned a retrieval and are no longer new."""
    now = time.time()
    usage = store.usage_map()
    results: list[tuple[str, float]] = []
    for row in store.conn.execute("SELECT path, mtime FROM notes"):
        path = row["path"]
        entry = usage.get(path)
        if entry and entry[0] > 0:
            continue
        if entry and entry[2]:  # pinned notes are never stale
            continue
        age_days = (now - float(row["mtime"])) / 86400.0
        if age_days >= STALE_AFTER_DAYS:
            results.append((path, round(age_days, 1)))
    results.sort(key=lambda row: row[1], reverse=True)
    return results[:limit]


# ------------------------------------------------------------- promotion --


@dataclass(slots=True)
class PromotionCandidate:
    tag: str
    entries: int
    paths: list[str] = field(default_factory=list)


def promotion_candidates(
    store: Store, *, journal_prefix: str = "journal/", min_entries: int = PROMOTION_MIN_ENTRIES
) -> list[PromotionCandidate]:
    """Themes that keep recurring in the journal and deserve a durable note.

    This is the episodic-to-semantic step: raw session notes accumulate under
    ``journal/``, and once the same tag appears often enough, the topic has
    earned a note of its own that search can rank as a first-class answer.
    """
    counts: Counter[str] = Counter()
    paths: dict[str, list[str]] = defaultdict(list)
    rows = store.conn.execute(
        "SELECT path, tags FROM notes WHERE path LIKE ? AND path NOT LIKE ?",
        (f"{journal_prefix}%", f"{SESSION_PREFIX}%"),
    )
    for row in rows:
        import json

        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue
        for tag in tags:
            counts[tag] += 1
            paths[tag].append(row["path"])

    existing = {r["title"] for r in store.conn.execute("SELECT title FROM notes WHERE path NOT LIKE ?", (f"{journal_prefix}%",))}
    candidates = [
        PromotionCandidate(tag=tag, entries=count, paths=sorted(paths[tag])[:10])
        for tag, count in counts.most_common()
        if count >= min_entries and tag not in existing
    ]
    return candidates


# ------------------------------------------------------- session capture --


@dataclass(slots=True)
class SessionRecord:
    """Deterministic account of one agent session's memory traffic."""

    started: float
    ended: float
    searches: int = 0
    unanswered: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    learned: int = 0

    @property
    def signals(self) -> int:
        """Events that taught the system something it did not already know."""
        return len(self.unanswered) + len(self.written) + self.learned

    def worth_recording(self) -> bool:
        return self.signals >= AUTO_CAPTURE_MIN_SIGNALS

    def render(self, *, client: str = "") -> str:
        import datetime

        def clock(stamp: float) -> str:
            return datetime.datetime.fromtimestamp(stamp).strftime("%H:%M")

        minutes = max(1, int((self.ended - self.started) // 60))
        who = f" ({client})" if client else ""
        lines = [f"## {clock(self.started)}-{clock(self.ended)} session{who}", ""]
        lines.append(
            f"{self.searches} search(es) over {minutes} min; "
            f"{len(self.written)} note(s) written, {self.learned} learning(s) recorded."
        )
        if self.unanswered:
            lines += ["", "Asked, not answered:"]
            lines += [f"- {question}" for question in self.unanswered[:AUTO_CAPTURE_MAX_QUESTIONS]]
            extra = len(self.unanswered) - AUTO_CAPTURE_MAX_QUESTIONS
            if extra > 0:
                lines.append(f"- ...and {extra} more")
        if self.written:
            lines += ["", "Touched:"]
            lines += [f"- [[{path.rsplit('/', 1)[-1].removesuffix('.md')}]]" for path in self.written[:10]]
        return "\n".join(lines)
