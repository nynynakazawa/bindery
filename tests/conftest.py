import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bindery.config import Config  # noqa: E402
from bindery.indexer import reindex  # noqa: E402
from bindery.store import Store  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    directory = tmp_path / "vault"
    directory.mkdir()
    return directory


@pytest.fixture
def config(vault, tmp_path):
    return Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)


@pytest.fixture
def indexed(config):
    def _build() -> Store:
        store = Store(config.db_path)
        reindex(config, store)
        return store

    return _build
