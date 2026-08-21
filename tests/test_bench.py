"""The benchmark has to keep running, or it stops being evidence.

Deliberately not asserting scores: the corpus is small enough that a real
retrieval change would move them, and a test that fails whenever retrieval
improves is a test people delete. What is checked is that the harness still
builds, still scores, and still agrees with itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench"
sys.path.insert(0, str(BENCH))

from corpus import NOTES, build  # noqa: E402
from queries import CASES  # noqa: E402
from run import Result, _rank_of, evaluate  # noqa: E402


class _Hit:
    def __init__(self, path):
        self.chunk = type("C", (), {"path": path})()


def test_every_expected_note_exists_in_the_corpus():
    """A case pointing at a note nobody wrote scores zero forever."""
    paths = {rel for rel, *_ in NOTES}
    for case in CASES:
        for expected in case.expect:
            assert expected in paths, f"{case.query!r} expects missing note {expected}"
        for forbidden in case.forbid:
            assert forbidden in paths


def test_a_case_with_no_answer_is_scored_on_returning_nothing():
    """Inventing a confident answer is a failure, not a neutral outcome."""
    assert _rank_of([], []) == 1
    assert _rank_of([_Hit("something.md")], []) is None


def test_rank_is_the_position_of_the_first_acceptable_answer():
    hits = [_Hit("a.md"), _Hit("b.md"), _Hit("c.md")]
    assert _rank_of(hits, ["b.md"]) == 2
    assert _rank_of(hits, ["c.md", "b.md"]) == 2
    assert _rank_of(hits, ["z.md"]) is None


def test_metrics_agree_with_the_ranks_they_summarise():
    result = Result()
    for rank in (1, 2, None, 5):
        result.record(CASES[0], rank, tokens=10, latency=1.0, leaked=False)

    assert result.recall1 == pytest.approx(0.25)
    assert result.recall5 == pytest.approx(0.75)
    assert result.mrr == pytest.approx((1 + 0.5 + 0 + 0.2) / 4)


def test_the_harness_runs_end_to_end_without_a_model(tmp_path, monkeypatch):
    """Keyword-only, so the suite never loads 220MB of ONNX."""
    from bindery.config import Config
    from bindery.indexer import reindex
    from bindery.store import Store

    vault, state = tmp_path / "vault", tmp_path / "state"
    vault.mkdir()
    build(vault)
    config = Config.resolve(vault=vault, state_dir=state, semantic=False, project="alpha")
    store = Store(config.db_path)
    reindex(config, store)
    store.close()

    result = evaluate("keyword", {"semantic": False, "scope": "all"}, vault, state)

    assert len(result.ranks) == len(CASES)
    assert 0.0 <= result.recall5 <= 1.0
    # Keyword retrieval must at least find an exact identifier.
    assert result.by_category["exact-identifier"][0] == 1
