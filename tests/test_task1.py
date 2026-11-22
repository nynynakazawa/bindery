"""Focused coverage for the agy and safe read-only status changes."""

from __future__ import annotations

import hashlib
import tomllib

from bindery.cli import _client_targets, main
from bindery.config import Config
from bindery.indexer import reindex
from bindery.store import Store


def test_antigravity_gets_boundary_in_args_and_environment(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(
        vault=vault,
        state_dir=tmp_path / "state",
        semantic=False,
        include=["work"],
        exclude=["private"],
        project="alpha",
    )
    # An explicitly named project travels in the arguments; a detected one does not.
    entry = _client_targets(config, pinned_project="alpha")["antigravity"]

    assert entry["env"] == {
        "BINDERY_VAULT": str(vault),
        "BINDERY_INCLUDE": "work",
        "BINDERY_EXCLUDE": "private",
    }
    assert entry["args"][-1] == "serve"
    args = entry["args"]
    assert args[args.index("--vault") + 1] == str(vault)
    assert args[args.index("--project") + 1] == "alpha"
    assert args[args.index("--include") + 1] == "work"
    assert args[args.index("--exclude") + 1] == "private"


def test_codex_toml_escapes_path_characters(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    vault = tmp_path / 'vault"slash\\line\nend'
    vault.mkdir()
    state = tmp_path / "state"

    assert main([
        "install", "codex", "--write", "--no-semantic",
        "--vault", str(vault), "--state-dir", str(state),
    ]) == 0
    payload = tomllib.loads((home / ".codex" / "config.toml").read_text())
    args = payload["mcp_servers"]["bindery"]["args"]
    assert args[args.index("--vault") + 1] == str(vault)


def test_status_does_not_create_index_without_vault(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "state"
    assert main(["status", "--no-semantic", "--state-dir", str(state)]) == 1
    assert "未索引" in capsys.readouterr().out
    assert not state.exists()


def test_status_does_not_change_existing_db(tmp_path, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "state"
    config = Config.resolve(vault=vault, state_dir=state, semantic=False)
    store = Store(config.db_path)
    store.close()
    before = hashlib.sha256(config.db_path.read_bytes()).digest()

    assert main([
        "status", "--no-semantic", "--vault", str(vault),
        "--state-dir", str(state),
    ]) == 1
    after = hashlib.sha256(config.db_path.read_bytes()).digest()
    assert before == after
    capsys.readouterr()


def test_indexer_ignores_markdown_symlink_outside_vault(tmp_path):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("# Secret\n\nprivate\n")
    (vault / "link.md").symlink_to(outside / "secret.md")
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    store = Store(config.db_path)
    report = reindex(config, store)
    assert report.scanned == 0
    assert store.stats()["notes"] == 0
    store.close()


def test_antigravity_prompt_writes_gemini_global_file_and_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    assert main(["prompt", "--global", "--write"]) == 0
    target = home / ".gemini" / "GEMINI.md"
    first = target.read_text()
    assert "Shared memory (Bindery)" in first
    assert main(["prompt", "--global", "--write"]) == 0
    assert target.read_text() == first


def test_invalid_json_install_is_nonzero_and_keeps_original(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini" / "config").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    path = home / ".gemini" / "config" / "mcp_config.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()

    assert main([
        "install", "antigravity", "--write", "--no-semantic",
        "--vault", str(tmp_path / "vault"), "--state-dir", str(tmp_path / "state"),
    ]) == 1
    assert path.read_bytes() == before


def test_config_update_retries_after_external_change_and_preserves_bak(tmp_path, monkeypatch):
    import bindery.safeio as safeio

    path = tmp_path / "settings.json"
    path.write_text("old", encoding="utf-8")
    plain_backup = path.with_suffix(".json.bak")
    plain_backup.write_text("older-backup", encoding="utf-8")
    original_snapshot = safeio._file_snapshot
    calls = 0

    def race(target):
        nonlocal calls
        result = original_snapshot(target)
        calls += 1
        if target == path and calls == 1:
            target.write_text("external", encoding="utf-8")
        return result

    monkeypatch.setattr(safeio, "_file_snapshot", race)
    safeio.atomic_config_update(
        path, lambda current: current + "-updated", lock_dir=tmp_path / "state"
    )

    assert path.read_text(encoding="utf-8") == "external-updated"
    assert plain_backup.read_text(encoding="utf-8") == "older-backup"
    backups = list(tmp_path.glob("settings.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "external"



def test_install_does_not_pin_the_project_detected_where_it_ran(tmp_path, monkeypatch):
    from bindery.cli import _client_targets
    from bindery.config import Config

    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", project="whatever-cwd-said")
    for spec in _client_targets(config, "user").values():
        assert "--project" not in spec["args"]
    pinned = _client_targets(config, "user", pinned_project="alpha")
    assert pinned["claude"]["args"][pinned["claude"]["args"].index("--project") + 1] == "alpha"
