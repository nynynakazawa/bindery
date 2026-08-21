"""The optional nearest-neighbour index.

Every property here is about it being *optional*: the same query must return
the same kind of answer whether or not the extension loaded, because it will
not load on every machine and must never be the reason semantic search stops
working.
"""

from __future__ import annotations

import pytest

from bindery.config import Config
from bindery.indexer import refresh_embeddings, reindex
from bindery.search import search
from bindery.store import Store


class DirectionalBackend:
    """Embeds on one axis per keyword, so nearest-neighbour order is knowable.

    The axes overlap slightly on purpose. Orthogonal vectors would make every
    non-matching note equidistant, and comparing two rankings of tied scores
    tests the tie-break rather than the index.
    """

    name = "directional"
    dim = 3

    #: Deliberately distinct similarities, so ordering is total.
    _AXES = {"りんご": (1.0, 0.3, 0.0), "みかん": (0.3, 1.0, 0.2), "ぶどう": (0.0, 0.2, 1.0)}

    def encode(self, texts):
        vectors = []
        for text in texts:
            found = next((v for k, v in self._AXES.items() if k in text), (0.1, 0.1, 0.1))
            vectors.append(list(found))
        return vectors


@pytest.fixture
def backend(monkeypatch):
    instance = DirectionalBackend()
    monkeypatch.setattr("bindery.embed.load_backend", lambda: instance)
    return instance


def _build(tmp_path, project=""):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    config = Config.resolve(
        vault=vault, state_dir=tmp_path / "state", semantic=True, project=project
    )
    return config, vault


def _note(vault, rel, text, project=None):
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nproject: {project}\n---\n\n" if project is not None else ""
    target.write_text(f"{front}{text}", encoding="utf-8")


def test_the_index_is_used_when_available(tmp_path, backend):
    config, vault = _build(tmp_path)
    _note(vault, "a.md", "# りんご\n\nりんごの話。\n")
    store = Store(config.db_path)
    reindex(config, store)
    refresh_embeddings(config, store)

    if not store.ann_enabled:
        pytest.skip("sqlite-vec not loadable in this build")

    assert store.nearest_vectors([1.0, 0.0, 0.0], 5)


def test_results_match_the_exact_scan(tmp_path, backend, monkeypatch):
    """The index is an optimisation. If it changes the answer it is a bug."""
    config, vault = _build(tmp_path)
    for name, fruit in (("a", "りんご"), ("b", "みかん"), ("c", "ぶどう")):
        _note(vault, f"{name}.md", f"# {fruit}\n\n{fruit}について。\n")
    store = Store(config.db_path)
    reindex(config, store)
    refresh_embeddings(config, store)

    if not store.ann_enabled:
        pytest.skip("sqlite-vec not loadable in this build")

    with_index, _ = search(config, store, "みかん", learn=False, scope="all")

    monkeypatch.setattr(store, "nearest_vectors", lambda *a, **k: None)
    without_index, _ = search(config, store, "みかん", learn=False, scope="all")

    assert [h.chunk.path for h in with_index] == [h.chunk.path for h in without_index]


def test_search_still_works_without_the_extension(tmp_path, backend, monkeypatch):
    config, vault = _build(tmp_path)
    _note(vault, "a.md", "# りんご\n\nりんごの話。\n")
    store = Store(config.db_path)
    monkeypatch.setattr(store, "nearest_vectors", lambda *a, **k: None)
    reindex(config, store)
    refresh_embeddings(config, store)

    hits, _ = search(config, store, "りんご", learn=False, scope="all")
    assert [h.chunk.path for h in hits] == ["a.md"]


def test_the_index_respects_project_scope(tmp_path, backend):
    """A nearest-neighbour query cannot express the filter, so it is applied after."""
    config, vault = _build(tmp_path, project="alpha")
    _note(vault, "alpha/a.md", "# りんご\n\nりんごの話。\n", project="alpha")
    _note(vault, "beta/b.md", "# りんご\n\nりんごの別の話。\n", project="beta")
    store = Store(config.db_path)
    reindex(config, store)
    refresh_embeddings(config, store)

    if not store.ann_enabled:
        pytest.skip("sqlite-vec not loadable in this build")

    hits, _ = search(config, store, "りんご", learn=False, scope="project")
    assert [h.chunk.path for h in hits] == ["alpha/a.md"]


def test_deleting_a_note_clears_its_vectors(tmp_path, backend):
    config, vault = _build(tmp_path)
    _note(vault, "a.md", "# りんご\n\nりんごの話。\n")
    store = Store(config.db_path)
    reindex(config, store)
    refresh_embeddings(config, store)

    (vault / "a.md").unlink()
    reindex(config, store)

    assert store.stats()["vectors"] == 0
    if store.ann_enabled:
        assert store.nearest_vectors([1.0, 0.0, 0.0], 5) == []


def test_the_index_can_be_rebuilt_from_the_stored_vectors(tmp_path, backend):
    """`vectors` is canonical; the ANN table is derived and disposable."""
    config, vault = _build(tmp_path)
    _note(vault, "a.md", "# りんご\n\nりんごの話。\n")
    store = Store(config.db_path)
    reindex(config, store)
    refresh_embeddings(config, store)

    if not store.ann_enabled:
        pytest.skip("sqlite-vec not loadable in this build")

    assert store.rebuild_vector_index() == store.stats()["vectors"]
    assert store.nearest_vectors([1.0, 0.0, 0.0], 5)
