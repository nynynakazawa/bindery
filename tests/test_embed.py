"""Choosing an embedding model.

The failure this guards against is silent: a hard-coded model name that the
library later dropped, an exception swallowed by `load_backend`, and semantic
search reporting itself as "not installed" on a machine where it was.
"""

from __future__ import annotations

import pytest

import bindery.embed as embed


class FakeTextEmbedding:
    """Stands in for fastembed, with a registry that can be varied."""

    supported: list[dict] = []
    constructed: list[str] = []

    def __init__(self, model_name: str):
        if model_name not in {m["model"] for m in self.supported}:
            raise ValueError(f"Model {model_name} is not supported in TextEmbedding.")
        FakeTextEmbedding.constructed.append(model_name)
        self.model_name = model_name

    def embed(self, texts):
        return [[0.1] * 384 for _ in texts]

    @classmethod
    def list_supported_models(cls):
        return cls.supported


@pytest.fixture(autouse=True)
def reset():
    embed.reset_backend()
    FakeTextEmbedding.constructed = []
    yield
    embed.reset_backend()


import sys  # noqa: E402
import types  # noqa: E402


def _fake_fastembed(monkeypatch, supported):
    FakeTextEmbedding.supported = supported
    module = types.ModuleType("fastembed")
    module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)


def test_the_first_supported_model_is_used(monkeypatch):
    _fake_fastembed(monkeypatch, [
        {"model": embed.MULTILINGUAL_MODELS[0], "dim": 384},
        {"model": embed.MULTILINGUAL_MODELS[1], "dim": 384},
    ])
    backend = embed._FastEmbedBackend()
    assert backend.model_name == embed.MULTILINGUAL_MODELS[0]
    assert backend.dim == 384


def test_an_unsupported_first_choice_falls_through(monkeypatch):
    """Exactly what happened: the pinned model left the registry."""
    _fake_fastembed(monkeypatch, [{"model": embed.MULTILINGUAL_MODELS[2], "dim": 768}])
    backend = embed._FastEmbedBackend()
    assert backend.model_name == embed.MULTILINGUAL_MODELS[2]
    assert backend.dim == 768
    assert embed.MULTILINGUAL_MODELS[0] not in FakeTextEmbedding.constructed


def test_no_supported_model_is_an_error_not_a_shrug(monkeypatch):
    _fake_fastembed(monkeypatch, [{"model": "some/english-only-model", "dim": 384}])
    with pytest.raises(RuntimeError):
        embed._FastEmbedBackend()


def test_every_candidate_is_multilingual():
    """An English-only model cannot embed the Japanese half of a vault."""
    for name in embed.MULTILINGUAL_MODELS:
        assert "multilingual" in name.lower() or "e5" in name.lower()


def test_the_backend_is_resolved_once(monkeypatch):
    """Constructing it loads an ONNX model from disk; a search must not repeat that."""
    _fake_fastembed(monkeypatch, [{"model": embed.MULTILINGUAL_MODELS[0], "dim": 384}])
    first = embed.load_backend()
    second = embed.load_backend()
    assert first is second
    assert FakeTextEmbedding.constructed == [embed.MULTILINGUAL_MODELS[0]]


def test_a_missing_backend_is_remembered_too(monkeypatch):
    """Otherwise every search retries two failing imports."""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert embed.load_backend() is None
    assert embed.load_backend() is None
