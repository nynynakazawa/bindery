"""Ranking and chunking quality.

The cases here are the ones that were actually wrong: a query one word longer
than the note returning nothing, a two-line note outranking a design document
because it was shorter, and a passage labelled with a heading so deep it no
longer said what it was about.
"""

from __future__ import annotations

import pytest

from bindery.config import Config
from bindery.indexer import chunk_markdown, reindex
from bindery.search import search
from bindery.store import Store


@pytest.fixture
def vault(tmp_path):
    directory = tmp_path / "vault"
    directory.mkdir()
    return directory


@pytest.fixture
def indexed(vault, tmp_path):
    def _build():
        config = Config.resolve(
            vault=vault, state_dir=tmp_path / "state", semantic=False, project=""
        )
        store = Store(config.db_path)
        reindex(config, store)
        return config, store

    return _build


def _note(vault, rel, text):
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _paths(hits):
    return [hit.chunk.path for hit in hits]


# --------------------------------------------------------------- ranking


def test_an_extra_word_does_not_empty_the_result(vault, indexed):
    """`依存を増やさない理由` found nothing while `依存を増やさない` found the answer."""
    _note(vault, "a.md", "# 方針\n\n依存を増やさない。実行時依存はゼロにした。\n")
    config, store = indexed()

    hits, _ = search(config, store, "依存を増やさない理由", learn=False)
    assert _paths(hits) == ["a.md"]


def test_every_term_matching_outranks_only_some(vault, indexed):
    """Recall without precision is what OR-only ranking bought."""
    _note(vault, "all.md", "# 設計\n\n認証の設計に Firebase を採用した。\n")
    _note(vault, "some.md", "# 雑記\n\nFirebase の話をまた聞いた。\n")
    config, store = indexed()

    hits, _ = search(config, store, "認証 設計 Firebase", learn=False)
    assert _paths(hits)[0] == "all.md"
    # the weaker match is still reachable, not discarded
    assert "some.md" in _paths(hits)


def test_a_short_note_does_not_outrank_a_real_answer(vault, indexed):
    """Ordering by chunk length put '認証済み。' above the design document."""
    _note(vault, "stub.md", "# メモ\n\n認証済み。\n")
    _note(
        vault,
        "design.md",
        "---\ntitle: 認証方式の設計\n---\n\n# 認証方式の設計\n\n"
        "大学メール認証を採用する。理由は本人性の担保と運用コスト。\n",
    )
    config, store = indexed()

    hits, _ = search(config, store, "認証", learn=False)
    assert _paths(hits)[0] == "design.md"


def test_the_title_is_searchable_even_when_the_body_never_says_it(vault, indexed):
    """In a personal vault the note's name is often the most precise statement."""
    _note(
        vault,
        "note.md",
        "---\ntitle: レートリミット設計\n---\n\n# 概要\n\n毎分100回までに絞る。\n",
    )
    config, store = indexed()

    hits, _ = search(config, store, "レートリミット", learn=False)
    assert _paths(hits) == ["note.md"]


def test_tags_are_searchable(vault, indexed):
    _note(
        vault,
        "note.md",
        "---\ntitle: 概要\ntags: [observability]\n---\n\n# 概要\n\n本文。\n",
    )
    config, store = indexed()

    hits, _ = search(config, store, "observability", learn=False)
    assert _paths(hits) == ["note.md"]


# -------------------------------------------------------------- chunking


def test_a_passage_carries_its_heading_trail(vault, indexed):
    """'Refresh token' alone has lost what it is a refresh token for."""
    _note(
        vault,
        "auth.md",
        "# Auth\n\nintro\n\n## Backend\n\nmid\n\n### Refresh token\n\n"
        "リフレッシュトークンは30日で失効する。\n",
    )
    config, store = indexed()

    hits, _ = search(config, store, "リフレッシュトークン", learn=False)
    assert hits[0].chunk.breadcrumb == "Auth / Backend / Refresh token"


def test_a_sibling_heading_replaces_rather_than_nests(vault, indexed):
    _note(vault, "n.md", "# A\n\n## B\n\nb body\n\n## C\n\nc body ここだけ固有\n")
    config, store = indexed()

    hits, _ = search(config, store, "ここだけ固有", learn=False)
    assert hits[0].chunk.breadcrumb == "A / C"


def test_a_code_block_is_never_split():
    body = "# Setup\n\n```python\n" + "\n".join(f"line_{i} = {i}" for i in range(400)) + "\n```\n"
    chunks = chunk_markdown(body, max_tokens=50, overlap=10)

    joined = [text for _crumb, text, _tokens in chunks]
    opens = sum(part.count("```") for part in joined)
    # The fence survives as a pair inside one chunk rather than being cut.
    assert opens == 2
    assert sum(1 for part in joined if "```python" in part) == 1


def test_a_table_is_never_split():
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(200))
    body = f"# T\n\n| a | b |\n| --- | --- |\n{rows}\n"
    chunks = chunk_markdown(body, max_tokens=40, overlap=5)

    with_rows = [text for _c, text, _t in chunks if "| r0 |" in text]
    assert len(with_rows) == 1
    assert "| r199 |" in with_rows[0], "the table was cut in half"


def test_a_list_item_keeps_its_continuation():
    body = "# L\n\n- first item\n  continued here\n- second item\n  also continued\n"
    chunks = chunk_markdown(body, max_tokens=6, overlap=0)

    for _crumb, text, _tokens in chunks:
        if "first item" in text:
            assert "continued here" in text


def test_a_comment_in_a_code_block_is_not_a_heading():
    body = "# Real\n\n```bash\n# not a heading\necho hi\n```\n\nafter\n"
    chunks = chunk_markdown(body, max_tokens=500, overlap=0)

    assert {crumb for crumb, _t, _n in chunks} == {"Real"}


def test_prose_still_splits_when_it_is_too_long():
    body = "# P\n\n" + "\n".join(f"文章の {i} 行目です。" for i in range(300))
    chunks = chunk_markdown(body, max_tokens=60, overlap=10)

    assert len(chunks) > 1
    assert all(crumb == "P" for crumb, _t, _n in chunks)
