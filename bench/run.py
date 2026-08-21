"""Measure retrieval, so that changing it is an argument about numbers.

Run it:

    python bench/run.py                 # every configuration
    python bench/run.py --only hybrid   # one of them

The point is not the absolute scores - the corpus is small and hand-written,
so they are not comparable to anything published. The point is the column
differences: whether adding semantic search to the keyword index actually
retrieves more than the keyword index alone on the kind of question a coding
agent asks, and whether project scoping costs anything to get leakage to zero.

If a configuration does not earn its place here, it should not be the default.
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus import build  # noqa: E402
from queries import CASES  # noqa: E402

from bindery.config import Config  # noqa: E402
from bindery.indexer import refresh_embeddings, reindex  # noqa: E402
from bindery.search import search  # noqa: E402
from bindery.store import Store  # noqa: E402

#: The configurations worth comparing. Each is a claim: that the thing it adds
#: pays for itself.
CONFIGURATIONS = {
    "keyword": dict(semantic=False, scope="all"),
    "semantic": dict(semantic=True, scope="all", keyword=False),
    "hybrid": dict(semantic=True, scope="all"),
    "hybrid+scope": dict(semantic=True, scope="project"),
}


class Result:
    def __init__(self) -> None:
        self.ranks: list[int | None] = []
        self.tokens: list[int] = []
        self.latencies: list[float] = []
        self.leaks = 0
        self.by_category: dict[str, list[int | None]] = {}

    def record(self, case, rank: int | None, tokens: int, latency: float, leaked: bool):
        self.ranks.append(rank)
        self.tokens.append(tokens)
        self.latencies.append(latency)
        self.leaks += int(leaked)
        self.by_category.setdefault(case.category, []).append(rank)

    @staticmethod
    def _recall(ranks, at: int) -> float:
        scored = [r for r in ranks]
        if not scored:
            return 0.0
        return sum(1 for r in scored if r is not None and r <= at) / len(scored)

    @property
    def recall1(self) -> float:
        return self._recall(self.ranks, 1)

    @property
    def recall5(self) -> float:
        return self._recall(self.ranks, 5)

    @property
    def mrr(self) -> float:
        if not self.ranks:
            return 0.0
        return sum(0.0 if r is None else 1.0 / r for r in self.ranks) / len(self.ranks)


def _rank_of(hits, expect: list[str]) -> int | None:
    """Where the first acceptable answer appeared, 1-based.

    A case with no expected answer is scored as correct when nothing came
    back - inventing a confident answer to a question the memory cannot
    answer is a failure, not a neutral outcome.
    """
    if not expect:
        return 1 if not hits else None
    for position, hit in enumerate(hits, start=1):
        if hit.chunk.path in expect:
            return position
    return None


def evaluate(name: str, options: dict, vault: Path, state: Path) -> Result:
    result = Result()
    for case in CASES:
        config = Config.resolve(
            vault=vault,
            state_dir=state,
            semantic=options["semantic"],
            project=case.project,
        )
        store = Store(config.db_path)
        started = time.perf_counter()
        hits, meta = search(
            config, store, case.query,
            learn=False, scope=options["scope"],
        )
        elapsed = (time.perf_counter() - started) * 1000
        if options.get("keyword") is False:
            # Semantic-only: keep just the passages the vector ranking found.
            hits = [h for h in hits if "semantic" in h.matched_by]
        leaked = any(h.chunk.path in case.forbid for h in hits)
        result.record(case, _rank_of(hits, case.expect), meta["tokens"], elapsed, leaked)
        store.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Run one configuration by name.")
    parser.add_argument("--keep", action="store_true", help="Leave the corpus on disk.")
    args = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix="bindery-bench-"))
    vault, state = workspace / "vault", workspace / "state"
    vault.mkdir(parents=True)
    notes = build(vault)

    config = Config.resolve(vault=vault, state_dir=state, semantic=True, project="alpha")
    store = Store(config.db_path)
    report = reindex(config, store)
    print(f"corpus: {notes} notes, {report.scanned} scanned, {store.stats()['chunks']} passages")
    embedded = refresh_embeddings(config, store)
    print(f"embedded: {embedded} passage(s); vector index: "
          f"{'sqlite-vec' if store.ann_enabled else 'exact scan'}")
    store.close()

    names = [args.only] if args.only else list(CONFIGURATIONS)
    results: dict[str, Result] = {}
    for name in names:
        if name not in CONFIGURATIONS:
            print(f"unknown configuration: {name}", file=sys.stderr)
            return 2
        results[name] = evaluate(name, CONFIGURATIONS[name], vault, state)

    print()
    header = f"{'configuration':<16}{'R@1':>7}{'R@5':>7}{'MRR':>7}{'tok/q':>8}{'p50 ms':>9}{'leak':>6}"
    print(header)
    print("-" * len(header))
    for name, result in results.items():
        print(
            f"{name:<16}"
            f"{result.recall1:>7.2f}{result.recall5:>7.2f}{result.mrr:>7.2f}"
            f"{statistics.mean(result.tokens):>8.0f}"
            f"{statistics.median(result.latencies):>9.1f}"
            f"{result.leaks:>6}"
        )

    print("\nby category (Recall@5)")
    categories = sorted({case.category for case in CASES})
    print(f"{'category':<20}" + "".join(f"{name:>14}" for name in results))
    for category in categories:
        row = f"{category:<20}"
        for result in results.values():
            ranks = result.by_category.get(category, [])
            row += f"{Result._recall(ranks, 5):>14.2f}"
        print(row)

    if args.keep:
        print(f"\ncorpus kept at {workspace}")
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
