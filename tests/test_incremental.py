"""Indexing work should be proportional to what changed, not to the vault.

Both properties here failed the same way before: writing one entry triggered a
full rescan, and updating a note dropped its vectors without ever making new
ones. The first made recording a lesson cost more the more the memory knew;
the second made semantic search decay exactly as the memory grew.
"""

from __future__ import annotations

import pytest

from bindery.config import Config
from bindery.indexer import index_path, refresh_embeddings, reindex
from bindery.server import MemoryServer
from bindery.store import Store


def _call(server, name, args):
    """Invoke a tool the way the MCP layer does, without the transport."""
    return server.call_tool(name, args)


class FakeBackend:
    """Deterministic stand-in - the real model is 100 MB and not the point."""

    name = "fake"
    dim = 3

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += len(texts)
        return [[float(len(t)), 1.0, 0.0] for t in texts]


@pytest.fixture
def semantic(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr("bindery.embed.load_backend", lambda: backend)
    return backend


def _config(tmp_path, semantic=False, project="alpha"):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return Config.resolve(
        vault=vault, state_dir=tmp_path / "state", semantic=semantic, project=project
    )


# --------------------------------------------------------- incremental scan


def test_learning_does_not_rescan_the_vault(tmp_path, monkeypatch):
    """A one-line entry must not cost a walk of every note in the vault."""
    config = _config(tmp_path)
    server = MemoryServer(config)

    def explode(_config):
        raise AssertionError("memory_learn walked the whole vault")

    monkeypatch.setattr("bindery.indexer.iter_markdown", explode)
    text = _call(server, "memory_learn", {"content": "FTS5 を採用した。", "tags": ["db"]})

    assert "Recorded" in text
    # and it is immediately searchable, which is the point of indexing at all
    assert "FTS5" in _call(server, "memory_search", {"query": "FTS5"})


def test_writing_does_not_rescan_the_vault(tmp_path, monkeypatch):
    config = _config(tmp_path)
    server = MemoryServer(config)

    def explode(_config):
        raise AssertionError("memory_write walked the whole vault")

    monkeypatch.setattr("bindery.indexer.iter_markdown", explode)
    _call(server, "memory_write", {"path": "adr/db.md", "content": "SQLite を使う。"})

    assert "SQLite" in _call(server, "memory_search", {"query": "SQLite"})


def test_an_unchanged_file_is_not_reopened(tmp_path, monkeypatch):
    """The mtime check has to happen before the read, or it saves nothing."""
    config = _config(tmp_path)
    (config.vault / "a.md").write_text("# A\n\n本文。\n", encoding="utf-8")
    store = Store(config.db_path)
    reindex(config, store)

    def explode(*args, **kwargs):
        raise AssertionError("re-read a file whose mtime had not changed")

    monkeypatch.setattr("pathlib.Path.read_text", explode)
    report = reindex(config, store)

    assert report.unchanged == 1
    assert report.updated == 0


def test_a_rewritten_but_identical_file_is_not_reindexed(tmp_path):
    """mtime is the cheap filter; the digest is still what decides."""
    config = _config(tmp_path)
    note = config.vault / "a.md"
    note.write_text("# A\n\n本文。\n", encoding="utf-8")
    store = Store(config.db_path)
    reindex(config, store)

    import os
    import time

    time.sleep(0.01)
    os.utime(note, None)  # new mtime, same bytes

    report = reindex(config, store)
    assert report.unchanged == 1 and report.updated == 0

    # The refreshed mtime means the next scan skips it without a read at all.
    report = reindex(config, store)
    assert report.unchanged == 1


def test_index_path_removes_a_deleted_note(tmp_path):
    config = _config(tmp_path)
    note = config.vault / "a.md"
    note.write_text("# A\n\n本文。\n", encoding="utf-8")
    store = Store(config.db_path)
    reindex(config, store)
    assert store.stats()["notes"] == 1

    note.unlink()
    report = index_path(config, store, note)

    assert report.removed == 1
    assert store.stats()["notes"] == 0


def test_index_path_respects_the_boundary(tmp_path):
    config = _config(tmp_path)
    config.exclude = ["private"]
    target = config.vault / "private" / "x.md"
    target.parent.mkdir()
    target.write_text("# X\n\n秘密。\n", encoding="utf-8")

    store = Store(config.db_path)
    index_path(config, store, target)

    assert store.stats()["notes"] == 0


# ------------------------------------------------------------- embeddings


def test_a_written_note_is_embedded_immediately(tmp_path, semantic):
    config = _config(tmp_path, semantic=True)
    server = MemoryServer(config)
    _call(server, "memory_write", {"path": "adr/db.md", "content": "SQLite を使う。"})

    stats = server.store.stats()
    assert stats["chunks"] > 0
    assert stats["vectors"] == stats["chunks"]


def test_updating_a_note_does_not_leave_it_unembedded(tmp_path, semantic):
    """The decay this prevents: vectors dropped on update and never remade."""
    config = _config(tmp_path, semantic=True)
    server = MemoryServer(config)

    _call(server, "memory_write", {"path": "adr/db.md", "content": "SQLite を使う。"})
    before = server.store.stats()
    assert before["vectors"] == before["chunks"]

    _call(server, "memory_write", {"path": "adr/db.md", "content": "やはり DuckDB を使う。"})
    after = server.store.stats()

    assert after["chunks"] > 0
    assert after["vectors"] == after["chunks"], "update left passages without vectors"


def test_repeated_learning_keeps_full_coverage(tmp_path, semantic):
    config = _config(tmp_path, semantic=True)
    server = MemoryServer(config)

    for i in range(5):
        _call(server, "memory_learn", {"content": f"学び {i} 番目。", "tags": ["x"]})

    stats = server.store.stats()
    assert stats["vectors"] == stats["chunks"]


def test_embedding_is_skipped_when_semantic_is_off(tmp_path, semantic):
    config = _config(tmp_path, semantic=False)
    server = MemoryServer(config)
    _call(server, "memory_write", {"path": "adr/db.md", "content": "SQLite を使う。"})

    assert server.store.stats()["vectors"] == 0
    assert semantic.calls == 0


def test_a_failing_backend_never_loses_the_note(tmp_path, monkeypatch):
    class Broken:
        name = "broken"
        dim = 3

        def encode(self, texts):
            raise RuntimeError("model exploded")

    monkeypatch.setattr("bindery.embed.load_backend", lambda: Broken())
    config = _config(tmp_path, semantic=True)
    server = MemoryServer(config)

    text = _call(server, "memory_write", {"path": "adr/db.md", "content": "SQLite を使う。"})

    assert "Wrote" in text
    assert (config.vault / "adr" / "db.md").exists()
    assert "SQLite" in _call(server, "memory_search", {"query": "SQLite"})


def test_refresh_embeddings_can_target_one_note(tmp_path, semantic):
    config = _config(tmp_path, semantic=True)
    (config.vault / "a.md").write_text("# A\n\nあ。\n", encoding="utf-8")
    (config.vault / "b.md").write_text("# B\n\nい。\n", encoding="utf-8")
    store = Store(config.db_path)
    reindex(config, store)

    done = refresh_embeddings(config, store, store.chunk_ids_for("a.md"))

    assert done == len(store.chunk_ids_for("a.md"))
    assert store.stats()["vectors"] == done < store.stats()["chunks"]
