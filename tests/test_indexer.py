from bindery.indexer import chunk_markdown, parse_frontmatter, parse_note, reindex
from bindery.store import Store
from pathlib import Path


def test_frontmatter_is_split_from_body():
    meta, body = parse_frontmatter("---\ntitle: A\ntags: [x, y]\n---\n\n# H\n\ntext\n")
    assert meta["title"] == "A"
    assert body.startswith("# H")


def test_body_without_frontmatter_is_untouched():
    meta, body = parse_frontmatter("# H\n\ntext\n")
    assert meta == {}
    assert body.startswith("# H")


def test_title_falls_back_to_first_heading_then_filename(tmp_path):
    note = parse_note(Path("x.md"), "# 見出し\n\nbody")
    assert note.title == "見出し"
    assert parse_note(Path("stem.md"), "no heading").title == "stem"


def test_wikilinks_are_extracted_and_deduplicated():
    note = parse_note(Path("a.md"), "[[B]] then [[B]] and [[C|alias]] and [[D#section]]")
    assert note.links == ["B", "C", "D"]


def test_chunks_respect_the_token_ceiling():
    body = "# H\n\n" + "\n".join(f"line {i} " + "x" * 80 for i in range(60))
    chunks = chunk_markdown(body, max_tokens=100, overlap=10)
    assert len(chunks) > 1
    # Overlap can push a chunk slightly past the target; it must not run away.
    assert all(tokens <= 100 + 40 for _, _, tokens in chunks)


def test_headings_start_new_chunks():
    chunks = chunk_markdown("# One\n\nalpha\n\n# Two\n\nbeta", max_tokens=1000, overlap=0)
    assert [heading for heading, _, _ in chunks] == ["One", "Two"]


def test_reindex_is_incremental(config):
    (config.vault / "a.md").write_text("# A\n\nalpha", encoding="utf-8")
    store = Store(config.db_path)

    first = reindex(config, store)
    assert first.added == 1 and first.unchanged == 0

    second = reindex(config, store)
    assert second.added == 0 and second.unchanged == 1

    (config.vault / "a.md").write_text("# A\n\nbeta", encoding="utf-8")
    third = reindex(config, store)
    assert third.updated == 1


def test_deleted_notes_leave_the_index(config):
    target = config.vault / "gone.md"
    target.write_text("# Gone\n\ntext", encoding="utf-8")
    store = Store(config.db_path)
    reindex(config, store)
    assert store.stats()["notes"] == 1

    target.unlink()
    report = reindex(config, store)
    assert report.removed == 1
    assert store.stats()["notes"] == 0


def test_reindex_does_not_leave_orphaned_search_rows(config):
    """A stale full-text row would resurrect deleted content in results."""
    path = config.vault / "a.md"
    path.write_text("# A\n\noriginal content here", encoding="utf-8")
    store = Store(config.db_path)
    reindex(config, store)

    path.write_text("# A\n\nreplacement content here", encoding="utf-8")
    reindex(config, store)

    fts_rows = store.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    assert fts_rows == store.stats()["chunks"]
    hits = store.conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"original\"'"
    ).fetchall()
    assert hits == []
