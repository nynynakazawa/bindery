from bindery.search import search
from bindery.store import Store


def _write(config, name, text):
    (config.vault / name).write_text(text, encoding="utf-8")


def test_japanese_two_character_query_matches(config, indexed):
    """FTS5's trigram tokenizer cannot index a 2-character term.

    Japanese is full of them (認証 / 設計 / 課金), so a substring fallback runs
    for short terms. Without it these queries silently return nothing.
    """
    _write(config, "a.md", "# 認証方式の決定\n\nJWT を採用した。\n")
    store = indexed()
    hits, meta = search(config, store, "認証")
    assert meta["returned"] == 1
    assert hits[0].chunk.path == "a.md"


def test_japanese_longer_query_matches_via_full_text_index(config, indexed):
    _write(config, "a.md", "# 決定\n\n水平スケール時の共有ストアを回避する。\n")
    store = indexed()
    hits, _ = search(config, store, "共有ストア")
    assert hits and hits[0].chunk.path == "a.md"


def test_english_query_matches(config, indexed):
    _write(config, "b.md", "# Deployment\n\nWe promote the same artifact across environments.\n")
    store = indexed()
    hits, _ = search(config, store, "artifact")
    assert hits and hits[0].chunk.path == "b.md"


def test_absent_term_returns_nothing(config, indexed):
    _write(config, "a.md", "# A\n\nalpha\n")
    store = indexed()
    hits, meta = search(config, store, "totallyabsentterm")
    assert hits == [] and meta["returned"] == 0


def test_token_budget_is_a_hard_cap(config, indexed):
    for index in range(10):
        _write(config, f"n{index}.md", f"# Note {index}\n\nshared marker word " + "padding " * 60)
    store = indexed()

    generous, generous_meta = search(config, store, "marker", limit=10, max_tokens=100000)
    tight, tight_meta = search(config, store, "marker", limit=10, max_tokens=60)

    assert generous_meta["returned"] > tight_meta["returned"]
    assert tight_meta["tokens"] <= 60
    assert len(tight) <= len(generous)


def test_budget_overflow_is_reported_not_hidden(config, indexed):
    """A caller must be able to tell 'nothing matched' from 'too much matched'."""
    for index in range(5):
        _write(config, f"n{index}.md", f"# N{index}\n\nmarker " + "padding " * 80)
    store = indexed()
    hits, meta = search(config, store, "marker", limit=5, max_tokens=30)
    assert meta["considered"] > 0
    assert meta["truncated"] > 0 or hits


def test_limit_is_respected(config, indexed):
    for index in range(8):
        _write(config, f"n{index}.md", f"# N{index}\n\ncommonword here\n")
    store = indexed()
    hits, _ = search(config, store, "commonword", limit=3, max_tokens=100000)
    assert len(hits) <= 3


def test_query_syntax_characters_do_not_crash_the_index(config, indexed):
    _write(config, "a.md", "# A\n\nalpha beta\n")
    store = indexed()
    for hostile in ['"', "*", "(", ")", "^", "-", 'alpha" OR "', "NEAR(a b)", ""]:
        hits, meta = search(config, store, hostile)
        assert isinstance(meta["returned"], int)


def test_search_returns_the_passage_not_the_whole_note(config, indexed):
    """The point of chunking: a match in one section must not drag in the rest."""
    body = "# Title\n\n## Wanted\n\nthe needle is here\n\n## Unwanted\n\n" + "filler " * 400
    _write(config, "big.md", body)
    store = indexed()
    hits, meta = search(config, store, "needle", max_tokens=100000)
    assert hits
    assert "needle" in hits[0].chunk.body
    assert "filler" not in hits[0].chunk.body
