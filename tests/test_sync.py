"""Gate A coverage for encrypted Vault synchronisation."""

from __future__ import annotations

import json
import base64
import subprocess
import threading
from contextlib import contextmanager
import unicodedata
from pathlib import Path

import pytest

from bindery import sync
from bindery.cli import main
from bindery.config import Config


@pytest.fixture(autouse=True)
def non_keychain(monkeypatch):
    # The production macOS path uses Keychain. Gate A keeps that boundary
    # mocked by using the 0600 state-dir implementation.
    monkeypatch.setattr(sync.sys, "platform", "linux")


def _bare(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return remote


def _pair(a, b, remote, tmp_path):
    ca = Config.resolve(vault=a, state_dir=tmp_path / "state-a", semantic=False)
    cb = Config.resolve(vault=b, state_dir=tmp_path / "state-b", semantic=False)
    assert sync.init_sync(ca, str(remote), write=True).exit_code == 0
    assert sync.run_sync(ca).exit_code == 0
    pa, pb = sync.paths_for(ca), sync.paths_for(cb)
    state_a = json.loads(pa.state.read_text(encoding="utf-8"))
    state_b = {
        "configured": True,
        "remote": str(remote),
        "sync_id": state_a["sync_id"],
        "machine": sync.machine_label(cb),
    }
    pb.root.mkdir(parents=True)
    sync._initialise_repo(pb, sync.validate_remote(str(remote)), write=True)
    sync._ensure_format(pb, write=True)
    sync.store_key(cb, state_b, pb, sync.load_key(ca, state_a, pa))
    sync._save_state(pb, state_b)
    assert sync.run_sync(cb).exit_code == 0
    return ca, cb


def test_remote_url_policy_rejects_unsafe_transports():
    for value in ("-u", "ext::sh -c evil", "https://example.test/x", "ssh://u:pw@example.test/x"):
        with pytest.raises(sync.SyncError):
            sync.validate_remote(value)
    with pytest.raises(sync.SyncError):
        sync.validate_remote("github.com:user/repo.git")
    assert sync.validate_remote("file:///tmp/bindery.git").kind == "file"


def test_blob_authentication_and_path_names_are_not_plaintext():
    master = b"k" * 32
    blob = sync.encrypt_object(master, "sync-id", "a" * 32, b"private body")
    with pytest.raises(sync.SyncBlocked):
        sync.decrypt_object(master, "sync-id", "a" * 32, blob[:-1] + bytes([blob[-1] ^ 1]))
    assert b"private body" not in blob
    assert b"notes/private.md" not in blob


def test_two_vaults_sync_and_remote_contains_ciphertext_only(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "private-note.md").write_text("only on A", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)

    assert (b / "private-note.md").exists()
    assert sync.run_sync(cb).exit_code == 0
    object_ids = subprocess.run(
        ["git", "--git-dir", str(remote), "cat-file", "--batch-all-objects", "--batch-check"],
        capture_output=True, text=True, check=True,
    ).stdout
    objects = subprocess.run(
        ["git", "--git-dir", str(remote), "cat-file", "--batch"],
        input="".join(line.split()[0] + "\n" for line in object_ids.splitlines()).encode("ascii"),
        capture_output=True,
        check=True,
    ).stdout
    assert b"only on A" not in objects
    assert b"private-note.md" not in objects
    assert (sync.paths_for(ca).repo / ".git").is_dir()
    assert not (a / ".git").exists()


def test_binary_files_are_applied_as_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "image.bin").write_bytes(b"\x00\xff\x80binary")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    assert sync.run_sync(cb).exit_code == 0
    assert (b / "image.bin").read_bytes() == b"\x00\xff\x80binary"


def test_conflict_keeps_both_versions_and_converges(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "same.md").write_text("base", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "same.md").write_text("from A", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0
    (b / "same.md").write_text("from B", encoding="utf-8")
    result = sync.run_sync(cb)
    assert result.exit_code == 0
    conflicts = list(b.glob("same.conflict-*.md"))
    assert len(conflicts) == 1
    assert {(b / "same.md").read_text(), conflicts[0].read_text()} == {"from A", "from B"}
    parents = subprocess.run(
        ["git", "-C", str(sync.paths_for(cb).repo), "rev-list", "--parents", "-n", "1", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert len(parents) == 3
    assert sync.run_sync(cb).exit_code == 0
    assert len(list(b.glob("same.conflict-*.md"))) == 1


def test_conflict_copy_update_preserves_existing_version_in_same_plan(tmp_path):
    """An updated conflict copy must not overwrite the primary conflict loser."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote_version = b"aaa"
    loser_version = b"zzz"
    unrelated_version = b"unrelated-remote-change"
    assert sync._sha256(remote_version) > sync._sha256(loser_version)

    (a / "same.md").write_bytes(b"base")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    initial_copy = sync._conflict_path("same.md", sync._sha256(loser_version))
    (a / "same.md").write_bytes(b"base")
    (a / initial_copy).write_bytes(loser_version)
    assert sync.run_sync(ca).exit_code == 0

    (a / "same.md").write_bytes(remote_version)
    (a / initial_copy).write_bytes(unrelated_version)
    assert sync.run_sync(ca).exit_code == 0

    (b / "same.md").write_bytes(loser_version)
    assert sync.run_sync(cb).exit_code == 0

    preserved_copy = sync._conflict_path(initial_copy, sync._sha256(unrelated_version))
    assert (b / "same.md").read_bytes() == remote_version
    assert (b / initial_copy).read_bytes() == loser_version
    assert (b / preserved_copy).read_bytes() == unrelated_version

    # The hash-derived names are stable across a retry and do not multiply.
    assert sync.run_sync(cb).exit_code == 0
    assert sorted(path.name for path in b.glob("same*.md")) == sorted(
        ["same.md", Path(initial_copy).name, Path(preserved_copy).name]
    )


def test_secret_is_held_without_blocking_other_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "secret.md").write_text("API_KEY=supersecretvalue", encoding="utf-8")
    (a / "safe.md").write_text("safe", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    result = sync.sync_status(ca)
    assert result["held_secrets"] == 1
    assert (b / "safe.md").exists()
    assert not (b / "secret.md").exists()
    assert sync.run_sync(cb).exit_code == 0


def test_secret_held_conflict_copy_is_idempotent(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "note.md").write_text("base", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "note.md").write_text("remote-change", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0
    (b / "note.md").write_text("API_KEY=local-secret", encoding="utf-8")
    assert sync.run_sync(cb).exit_code == 0
    copies = list(b.glob("note.conflict-*.md"))
    assert len(copies) == 1
    (b / "held.md").write_text("API_KEY=fixture-secret", encoding="utf-8")
    assert main(["sync", "--json", "--vault", str(cb.vault), "--state-dir", str(cb.state_dir)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "held.md" in result["status"]["held_paths"]
    assert result["status"]["conflict_paths"] == [copies[0].name]
    assert len(list(b.glob("note.conflict-*.md"))) == 1


def test_json_contract_is_one_object_and_sync_status_is_non_secret(tmp_path, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "state"
    assert main(["status", "--json", "--no-semantic", "--vault", str(vault), "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert set(status) == {"version", "vault", "vault_exists", "indexed", "notes", "passages", "embedded", "semantic", "state_dir", "error"}
    assert main(["sync", "status", "--json", "--vault", str(vault), "--state-dir", str(state), "--no-semantic"]) == 0
    sync_status = json.loads(capsys.readouterr().out)
    assert set(sync_status) >= {"configured", "remote", "git", "index", "held_secrets", "error"}
    assert sync_status["key_store"] is None
    assert "master.key" not in json.dumps(sync_status).lower()


def test_scheduler_dry_run_uses_absolute_paths_and_escapes_percent(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sync.sys, "platform", "linux")
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    result = sync.install_scheduler(config, interval=301, write=False)
    assert result.exit_code == 0
    output = capsys.readouterr().out
    assert str(vault.resolve()) in output
    assert "ExecStart=" in output


def test_scheduler_write_and_uninstall_are_user_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(sync.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    calls = []

    class Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(command, **kwargs):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    assert sync.install_scheduler(config, interval=301, write=True).exit_code == 0
    service, timer = sync._scheduler_paths(config)[2:]
    assert service.exists() and timer is not None and timer.exists()
    assert any(command[:3] == ["systemctl", "--user", "daemon-reload"] for command in calls)
    assert sync.uninstall_scheduler(config, write=True).exit_code == 0
    assert not service.exists() and not timer.exists()


def test_conflict_transaction_contains_only_encrypted_refs_and_resumes_in_safe_order(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "same.md").write_text("base", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "same.md").write_text("aaa", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0
    (b / "same.md").write_text("zzz", encoding="utf-8")

    original_write = sync.atomic_write
    failed = False

    def stop_before_primary(path, data, **kwargs):
        nonlocal failed
        if Path(path) == b / "same.md" and not failed:
            failed = True
            raise OSError("simulated stop")
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(sync, "atomic_write", stop_before_primary)
    assert sync.run_sync(cb).exit_code == 1
    transaction = sync.paths_for(cb).transaction
    assert transaction.exists()
    raw = transaction.read_text(encoding="utf-8")
    assert "aaa" not in raw and "zzz" not in raw
    assert "object_id" in raw
    conflict = next(b.glob("same.conflict-*.md"))
    assert conflict.read_text(encoding="utf-8") in {"aaa", "zzz"}
    assert (b / "same.md").read_text(encoding="utf-8") == "zzz"

    monkeypatch.setattr(sync, "atomic_write", original_write)
    assert sync.run_sync(cb).exit_code == 0
    assert {b.joinpath("same.md").read_text(), conflict.read_text()} == {"aaa", "zzz"}
    assert not transaction.exists()


def test_apply_hash_mismatch_rescans_three_times_in_one_run(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "note.md").write_text("one", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "note.md").write_text("two", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0

    original_apply = sync._apply_plan
    calls = 0

    def mismatch_twice(config, paths, plan, master, sync_id):
        nonlocal calls
        calls += 1
        if calls < 3:
            paths.transaction.unlink(missing_ok=True)
            raise sync.PlanChanged("test race")
        return original_apply(config, paths, plan, master, sync_id)

    monkeypatch.setattr(sync, "_apply_plan", mismatch_twice)
    assert sync.run_sync(cb).exit_code == 0
    assert calls == 3
    assert (b / "note.md").read_text(encoding="utf-8") == "two"


def test_push_timeout_confirms_remote_commit_without_creating_another(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    remote = _bare(tmp_path)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0
    paths = sync.paths_for(config)
    commit = "a" * 40

    def timeout_git(*args, **kwargs):
        if args[1] == "push":
            raise sync.SyncTimeout("push timed out")
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sync, "_git", timeout_git)
    monkeypatch.setattr(sync, "_fetch", lambda _paths: None)
    monkeypatch.setattr(sync, "_remote_head", lambda _paths: commit)
    sync._push(paths, commit)


def test_failed_push_reuses_the_pending_commit_on_the_next_run(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca, _cb = _pair(a, b, remote, tmp_path)
    (a / "pending.md").write_text("pending", encoding="utf-8")
    original_push = sync._push
    monkeypatch.setattr(sync, "_push", lambda *args, **kwargs: (_ for _ in ()).throw(sync.SyncError("network")))
    assert sync.run_sync(ca).exit_code == 1
    paths = sync.paths_for(ca)
    failed_state = json.loads(paths.state.read_text(encoding="utf-8"))
    pending = failed_state["pending_commit"]
    monkeypatch.setattr(sync, "_push", original_push)
    assert sync.run_sync(ca).exit_code == 0
    recovered = json.loads(paths.state.read_text(encoding="utf-8"))
    assert recovered["accepted_manifest"] == pending["manifest_hash"]
    assert not recovered.get("pending_commit")


def test_three_terminals_converge_after_conflicting_updates(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for vault in (a, b, c):
        vault.mkdir()
    (a / "shared.md").write_text("base", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    _ca_again, cc = _pair(a, c, remote, tmp_path)
    (a / "shared.md").write_text("from-a", encoding="utf-8")
    (b / "shared.md").write_text("from-b", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0
    assert sync.run_sync(cb).exit_code == 0
    assert sync.run_sync(cc).exit_code == 0
    assert sync.run_sync(ca).exit_code == 0
    assert sync.run_sync(cb).exit_code == 0
    assert sync.run_sync(cc).exit_code == 0

    def visible(vault):
        return {
            str(path.relative_to(vault)): path.read_bytes()
            for path in vault.rglob("*")
            if path.is_file() and ".bindery-sync" not in path.parts
        }

    assert visible(a) == visible(b) == visible(c)
    assert len(list(a.glob("shared.conflict-*.md"))) == 1


@pytest.mark.parametrize("first,second", [("a", "b"), ("b", "a")])
def test_conflict_resolution_is_symmetric_to_sync_order(tmp_path, first, second):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "same.md").write_text("base", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "same.md").write_text("left", encoding="utf-8")
    (b / "same.md").write_text("right", encoding="utf-8")
    configs = {"a": ca, "b": cb}
    assert sync.run_sync(configs[first]).exit_code == 0
    assert sync.run_sync(configs[second]).exit_code == 0
    assert sync.run_sync(configs[first]).exit_code == 0
    assert sync.run_sync(configs[second]).exit_code == 0
    assert (a / "same.md").read_bytes() == (b / "same.md").read_bytes()
    copies_a = sorted(path.name for path in a.glob("same.conflict-*.md"))
    copies_b = sorted(path.name for path in b.glob("same.conflict-*.md"))
    assert copies_a == copies_b and len(copies_a) == 1


def test_change_delete_and_delete_delete_converge(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "note.md").write_text("base", encoding="utf-8")
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "note.md").unlink()
    (b / "note.md").write_text("changed", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0
    assert sync.run_sync(cb).exit_code == 0
    assert sync.run_sync(ca).exit_code == 0
    assert (a / "note.md").read_text() == (b / "note.md").read_text() == "changed"
    (a / "note.md").unlink()
    (b / "note.md").unlink()
    assert sync.run_sync(ca).exit_code == 0
    assert sync.run_sync(cb).exit_code == 0
    assert not (a / "note.md").exists() and not (b / "note.md").exists()


def test_concurrent_push_retries_and_preserves_both_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "a.md").write_text("A", encoding="utf-8")
    (b / "b.md").write_text("B", encoding="utf-8")
    results = []

    def run(config):
        results.append(sync.run_sync(config).exit_code)

    first = threading.Thread(target=run, args=(ca,))
    second = threading.Thread(target=run, args=(cb,))
    first.start()
    second.start()
    first.join(timeout=15)
    second.join(timeout=15)
    assert not first.is_alive() and not second.is_alive()
    assert all(code in {0, 1} for code in results)
    assert sync.run_sync(ca).exit_code == 0
    assert sync.run_sync(cb).exit_code == 0
    assert (a / "a.md").read_text() == (b / "a.md").read_text() == "A"
    assert (a / "b.md").read_text() == (b / "b.md").read_text() == "B"


def test_manifest_chain_rejects_disconnected_series_even_with_git_parent(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    paths = sync.paths_for(config)
    paths.root.mkdir(parents=True)
    sync._initialise_repo(paths, sync.validate_remote(str(_bare(tmp_path))), write=True)
    empty = {"files": {}}
    doc1 = {"generation": 1, "previous_hash": None, "files": {}}
    doc2 = {"generation": 2, "previous_hash": sync._manifest_hash(doc1), "files": {}}
    unrelated = {"generation": 2, "previous_hash": "f" * 64, "files": {}}
    doc3 = {"generation": 3, "previous_hash": sync._manifest_hash(unrelated), "files": {}}
    h1, h2, h3 = map(sync._manifest_hash, (doc1, doc2, doc3))
    state = {"accepted_generation": 1, "accepted_manifest": h1}
    monkeypatch.setattr(sync, "_manifest_candidates", lambda *args: {h2: (doc2, 2)})
    with pytest.raises(sync.SyncBlocked):
        sync._check_rollback(state, doc3, h3, 3, paths=paths, master=b"k" * 32, sync_id="id", remote_commit="parent")
    with pytest.raises(sync.SyncBlocked):
        sync._check_rollback({"accepted_generation": 1, "accepted_manifest": h1}, doc2, sync._manifest_hash({"generation": 2, "previous_hash": h1, "files": {"x": 1}}), 2, paths=paths, master=b"k" * 32, sync_id="id", remote_commit="parent")
    with pytest.raises(sync.SyncBlocked):
        sync._check_rollback({"accepted_generation": 3, "accepted_manifest": h3}, doc2, h2, 2)


def test_crypto_rejects_wrong_key_header_generation_and_aad_swaps():
    key = b"k" * 32
    other = b"o" * 32
    manifest = sync.encrypt_manifest(key, "sync", 7, sync._json_bytes({"generation": 7, "previous_hash": None, "files": {}}))
    with pytest.raises(sync.SyncBlocked):
        sync.decrypt_manifest(other, "sync", manifest)
    corrupted_header = bytearray(manifest)
    corrupted_header[19] ^= 1
    with pytest.raises(sync.SyncBlocked):
        sync.decrypt_manifest(key, "sync", bytes(corrupted_header))
    blob = sync.encrypt_object(key, "sync", "a" * 32, b"body")
    with pytest.raises(sync.SyncBlocked):
        sync.decrypt_object(key, "other-sync", "a" * 32, blob)
    with pytest.raises(sync.SyncBlocked):
        sync.decrypt_object(key, "sync", "b" * 32, blob)


def test_keychain_and_pair_keep_key_out_of_argv(tmp_path, monkeypatch):
    key = b"s" * 32
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(sync.sys, "platform", "darwin")
    real_run = sync.subprocess.run
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    sync._keychain_put("sync", key)
    assert all(base64.b64encode(key).decode() not in str(arg) for arg in calls[-1][0])
    assert base64.b64encode(key).decode() in calls[-1][1]["input"]
    assert sync._keychain_get("sync") != key

    monkeypatch.setattr(sync.sys, "platform", "linux")
    monkeypatch.setattr(sync.subprocess, "run", real_run)
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    remote = _bare(tmp_path)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0
    paths = sync.paths_for(config)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    actual_key = sync.load_key(config, state, paths)
    calls.clear()
    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    assert sync.pair(
        config,
        "host;$(touch BAD)",
        remote_vault=str(tmp_path / "peer;bad"),
        remote_bindery="/opt/bindery;bad",
        remote_key_store="file",
    ).exit_code == 0
    command, kwargs = calls[-1]
    assert all(base64.b64encode(actual_key).decode() not in str(arg) for arg in command)
    assert base64.b64encode(actual_key).decode() in kwargs["input"].decode("utf-8")
    assert command[-2] == "host;$(touch BAD)"
    assert "'/opt/bindery;bad'" in command[-1]
    assert "--key-store file" in command[-1]
    assert base64.b64encode(actual_key).decode() not in paths.state.read_text(encoding="utf-8")

    # The peer that hosts the bare repository reaches it by a local path, not
    # by the SSH alias this machine uses; --peer-remote carries that spelling.
    calls.clear()
    peer_path = str(tmp_path / "peer-side" / "remote.git")
    assert sync.pair(config, "host", peer_remote=peer_path).exit_code == 0
    sent = json.loads(calls[-1][1]["input"].decode("utf-8"))
    assert sent["remote"] == peer_path
    calls.clear()
    rejected = sync.pair(config, "host", peer_remote="ext::sh -c touch% /tmp/pwned")
    assert rejected.exit_code != 0
    assert calls == []


def test_auto_keychain_interaction_falls_back_to_private_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sync.sys, "platform", "darwin")
    vault = tmp_path / "vault"
    vault.mkdir()
    remote = _bare(tmp_path)
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    real_run = sync.subprocess.run

    class Completed:
        stdout = ""
        stderr = ""

        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **kwargs):
        if command[:2] == ["/usr/bin/security", "find-generic-password"]:
            return Completed(1)
        if command[:2] == ["/usr/bin/security", "-i"]:
            result = Completed(1)
            result.stderr = "User interaction is not allowed (-25308)"
            return result
        return real_run(command, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    result = sync.init_sync(config, str(remote), write=True)
    assert result.exit_code == 0
    paths = sync.paths_for(config)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    assert state["key_store"] == "file"
    assert paths.root.stat().st_mode & 0o777 == 0o700
    assert paths.key.stat().st_mode & 0o777 == 0o600
    assert sync.load_key(config, state, paths) == base64.b64decode(paths.key.read_text().strip())


def test_file_key_store_is_recorded_and_reloaded_on_macos(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    remote = _bare(tmp_path)
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    assert sync.init_sync(config, str(remote), write=True, key_store="file").exit_code == 0
    paths = sync.paths_for(config)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    key = sync.load_key(config, state, paths)
    assert state["key_store"] == "file"

    monkeypatch.setattr(sync.sys, "platform", "darwin")
    assert sync.load_key(config, state, paths) == key


def test_json_sync_failure_reports_error_on_stderr_and_in_json(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    state_dir = tmp_path / "state"
    remote = _bare(tmp_path)
    config = Config.resolve(vault=vault, state_dir=state_dir, semantic=False)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0
    real_run = sync.subprocess.run

    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"ssh: peer refused the pairing\n"

    def fake_run(command, **kwargs):
        if command and command[0] == "ssh":
            return Failed()
        return real_run(command, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    code = main([
        "sync", "pair", "host", "--json", "--no-semantic",
        "--vault", str(vault), "--state-dir", str(state_dir),
    ])
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out)
    assert "peer refused the pairing" in payload["status"]["error"]
    assert captured.err.count("\n") == 1
    assert "peer refused the pairing" in captured.err
    assert "master.key" not in captured.out


def test_accept_key_persists_remote_and_checkpoint_without_init(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca = Config.resolve(vault=a, state_dir=tmp_path / "state-a", semantic=False)
    cb = Config.resolve(vault=b, state_dir=tmp_path / "state-b", semantic=False)
    assert sync.init_sync(ca, str(remote), write=True).exit_code == 0
    assert sync.run_sync(ca).exit_code == 0
    pa = sync.paths_for(ca)
    state_a = json.loads(pa.state.read_text(encoding="utf-8"))
    payload = sync._json_bytes({
        "sync_id": state_a["sync_id"],
        "key": base64.b64encode(sync.load_key(ca, state_a, pa)).decode("ascii"),
        "remote": str(remote),
        "checkpoint": {
            "generation": state_a["accepted_generation"],
            "manifest_hash": state_a["accepted_manifest"],
        },
    })
    result = sync.accept_key(cb, state_a["sync_id"], payload)
    assert result.exit_code == 0
    state_b = json.loads(sync.paths_for(cb).state.read_text(encoding="utf-8"))
    assert state_b["remote"] == str(remote)
    assert state_b["accepted_generation"] == state_a["accepted_generation"]
    assert sync.run_sync(cb).exit_code == 0


def test_remote_url_connection_and_display_are_separate_and_hosts_normalized():
    remote = sync.validate_remote("ssh://Other@GITHUB.COM./vault", allow_public_host=True)
    assert remote.value == "ssh://Other@github.com/vault"
    assert remote.display == "ssh://***@github.com/vault"
    with pytest.raises(sync.SyncError):
        sync.validate_remote("file://host/tmp/repo")
    with pytest.raises(sync.SyncError):
        sync.validate_remote("-bad")
    with pytest.raises(sync.SyncError):
        sync.validate_remote("ssh://u@github.com.:22/repo")


def test_path_validation_rejects_all_destination_collisions_before_write(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    for value in ("/absolute.md", "../outside.md", "x\x00.md", ".git/config"):
        with pytest.raises(sync.SyncBlocked):
            sync._safe_rel(value)
    nfd = unicodedata.normalize("NFD", "é.md")
    with pytest.raises(sync.SyncBlocked):
        sync._safe_rel(nfd)
    plan = sync.SyncPlan(publish={"A.md": {}, "a.md": {}}, items=[])
    with pytest.raises(sync.SyncBlocked):
        sync._validate_plan_paths(config, plan, {"A.md", "a.md"})
    plan = sync.SyncPlan(publish={"x.md": {}, "x.md/y.md": {}}, items=[])
    with pytest.raises(sync.SyncBlocked):
        sync._validate_plan_paths(config, plan, set(plan.publish))
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "link").symlink_to(outside, target_is_directory=True)
    plan = sync.SyncPlan(publish={"link/note.md": {}}, items=[])
    with pytest.raises(sync.SyncBlocked):
        sync._validate_plan_paths(config, plan, set(plan.publish))


def test_index_failure_does_not_advance_applied_marker_and_recovers(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    (a / "fresh.md").write_text("fresh", encoding="utf-8")
    assert sync.run_sync(ca).exit_code == 0
    paths_b = sync.paths_for(cb)
    before = json.loads(paths_b.state.read_text(encoding="utf-8"))
    original_index = sync._index_after_sync
    monkeypatch.setattr(sync, "_index_after_sync", lambda *args: (_ for _ in ()).throw(RuntimeError("index down")))
    result = sync.run_sync(cb)
    assert result.exit_code == 1
    failed = json.loads(paths_b.state.read_text(encoding="utf-8"))
    assert failed.get("applied_generation") == before.get("applied_generation")
    assert failed.get("accepted_generation", 0) > failed.get("applied_generation", 0)
    monkeypatch.setattr(sync, "_index_after_sync", original_index)
    assert sync.run_sync(cb).exit_code == 0
    recovered = json.loads(paths_b.state.read_text(encoding="utf-8"))
    assert recovered["applied_generation"] == recovered["accepted_generation"]


def test_remote_secret_is_warned_and_applied_without_name_error(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    paths_a = sync.paths_for(ca)
    state_a = json.loads(paths_a.state.read_text(encoding="utf-8"))
    key = sync.load_key(ca, state_a, paths_a)
    sync_id = state_a["sync_id"]
    sync._fetch(paths_a)
    remote_head = sync._remote_head(paths_a)
    remote_info = sync._read_remote_manifest(paths_a, key, sync_id, remote_head)
    assert remote_info is not None
    remote_doc, remote_hash, remote_generation = remote_info
    secret = b"API_KEY=remote-secret"
    (a / "remote-secret.md").write_bytes(secret)
    entry = {"sha256": sync._sha256(secret), "size": len(secret), "machine": "remote"}
    files = sync._local_manifest_with_objects(ca, key, sync_id, {"remote-secret.md": entry})
    manifest_hash, _tree_hash, _count, _key_id, _committed = sync._write_encrypted_tree(
        paths_a,
        ca,
        key,
        sync_id,
        files,
        remote_generation + 1,
        remote_hash,
    )
    commit = sync._git_commit(paths_a, local_head=remote_head, remote_head=remote_head)
    sync._push(paths_a, commit)
    result = sync.run_sync(cb)
    assert result.exit_code == 0
    assert (b / "remote-secret.md").read_bytes() == secret
    assert "remote-secret.md" in result.status["remote_secret_paths"]


def test_rekey_failure_keeps_old_key_and_success_archives_it(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("note", encoding="utf-8")
    remote = _bare(tmp_path)
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0
    assert sync.run_sync(config).exit_code == 0
    paths = sync.paths_for(config)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    old_key = sync.load_key(config, state, paths)
    original_push = sync._push
    monkeypatch.setattr(sync, "_push", lambda *args, **kwargs: (_ for _ in ()).throw(sync.SyncError("push failed")))
    assert sync.rekey(config, write=True).exit_code == 1
    assert sync.load_key(config, state, paths) == old_key
    assert paths.repo.exists()
    monkeypatch.setattr(sync, "_push", original_push)
    assert sync.rekey(config, write=True).exit_code == 0
    new_state = json.loads(paths.state.read_text(encoding="utf-8"))
    new_key = sync.load_key(config, new_state, paths)
    assert new_key != old_key
    old_id = sync.derive_keys(old_key)[2].hex()
    new_id = sync.derive_keys(new_key)[2].hex()
    assert (paths.root / f"master.key.previous-{old_id}").exists()
    assert any(path.name.startswith("repo.previous-") for path in paths.root.iterdir())
    assert set(new_state["encryption_counts"]) == {new_id}


def test_rekey_preflights_normal_sync_before_replacing_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)

    (b / "only-b.md").write_text("unique remote data", encoding="utf-8")
    assert sync.run_sync(cb).exit_code == 0
    assert not (a / "only-b.md").exists()

    result = sync.rekey(ca, write=True)
    assert result.exit_code == 0
    assert (a / "only-b.md").read_text(encoding="utf-8") == "unique remote data"

    paths = sync.paths_for(ca)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    key = sync.load_key(ca, state, paths)
    sync._fetch(paths)
    document, _manifest_hash, _generation = sync._read_remote_manifest(
        paths, key, state["sync_id"], sync._remote_head(paths)
    )
    assert "only-b.md" in document["files"]


def test_rekey_aborts_when_remote_head_changes_before_force_push(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    remote = _bare(tmp_path)
    ca, cb = _pair(a, b, remote, tmp_path)
    paths_a = sync.paths_for(ca)
    state_before = json.loads(paths_a.state.read_text(encoding="utf-8"))
    old_key = sync.load_key(ca, state_before, paths_a)
    original_push = sync._push
    raced = False

    def push_with_peer_race(paths, commit, **kwargs):
        nonlocal raced
        if kwargs.get("force") and not raced:
            raced = True
            (b / "pushed-during-rekey.md").write_text("peer update", encoding="utf-8")
            assert sync.run_sync(cb).exit_code == 0
        return original_push(paths, commit, **kwargs)

    monkeypatch.setattr(sync, "_push", push_with_peer_race)
    result = sync.rekey(ca, write=True)
    assert result.exit_code != 0
    assert raced
    assert sync.load_key(ca, json.loads(paths_a.state.read_text(encoding="utf-8")), paths_a) == old_key

    sync._fetch(paths_a)
    state_b = json.loads(sync.paths_for(cb).state.read_text(encoding="utf-8"))
    assert sync._remote_head(paths_a) == state_b["base_commit"]


def test_rekey_purge_uses_the_common_git_safety_options(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    remote = _bare(tmp_path)
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0
    assert sync.run_sync(config).exit_code == 0
    calls = []

    def fake_bare(paths, git_dir, *args, **kwargs):
        calls.append((paths, git_dir, args))
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sync, "_git_bare", fake_bare)
    assert sync.rekey(config, write=True, purge_remote=True).exit_code == 0
    assert len(calls) == 2
    assert all("protocol.allow=never" in sync._git_options(call[0]) for call in calls)


def test_encryption_count_tracks_real_calls_per_key_and_noop_does_not_increment(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    remote = _bare(tmp_path)
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0
    assert sync.run_sync(config).exit_code == 0
    paths = sync.paths_for(config)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    key = sync.load_key(config, state, paths)
    key_id = sync.derive_keys(key)[2].hex()
    initial = state["encryption_counts"][key_id]
    assert sync.run_sync(config).exit_code == 0
    same = json.loads(paths.state.read_text(encoding="utf-8"))["encryption_counts"][key_id]
    assert same == initial
    (vault / "new.md").write_text("new", encoding="utf-8")
    assert sync.run_sync(config).exit_code == 0
    increased = json.loads(paths.state.read_text(encoding="utf-8"))["encryption_counts"][key_id]
    assert increased > same


def test_systemd_quote_escapes_dollar_percent_and_rejects_controls(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sync.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault$NAME%value"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    result = sync.install_scheduler(config, write=False)
    assert result.exit_code == 0
    output = capsys.readouterr().out
    assert "$$NAME" in output
    assert "%%value" in output

    bad_vault = tmp_path / "bad\npath"
    bad_vault.mkdir()
    bad = Config.resolve(vault=bad_vault, state_dir=tmp_path / "bad-state", semantic=False)
    assert sync.install_scheduler(bad, write=False, emit=False).exit_code == 1


def test_sync_returns_blocked_on_vault_lock_timeout_without_writing(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    remote = _bare(tmp_path)
    assert sync.init_sync(config, str(remote), write=True).exit_code == 0

    @contextmanager
    def timeout(*args, **kwargs):
        raise sync.LockTimeout("locked")
        yield

    monkeypatch.setattr(sync, "vault_lock", timeout)
    result = sync.run_sync(config)
    assert result.exit_code == 2
    assert not sync.paths_for(config).transaction.exists()



def test_sync_follows_a_remote_changed_by_a_later_pairing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("hello\n", encoding="utf-8")
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False)
    first = _bare(tmp_path / "first")
    assert sync.init_sync(config, str(first), write=True).exit_code == 0
    assert sync.run_sync(config).exit_code == 0

    # A later pairing rewrites the state with another spelling of the remote
    # (here: a different bare repository that already holds the same history).
    second = tmp_path / "second.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(first), str(second)], check=True)
    paths = sync.paths_for(config)
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    state["remote"] = str(second)
    state["remote_display"] = str(second)
    paths.state.write_text(json.dumps(state), encoding="utf-8")

    (vault / "later.md").write_text("after re-pair\n", encoding="utf-8")
    assert sync.run_sync(config).exit_code == 0
    url = subprocess.run(["git", "-C", str(paths.repo), "remote", "get-url", "bindery"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert url == str(second)
    ref = f"refs/heads/{sync.DEFAULT_BRANCH}"
    first_head = subprocess.run(["git", "-C", str(first), "rev-parse", ref], capture_output=True, text=True, check=True).stdout
    second_head = subprocess.run(["git", "-C", str(second), "rev-parse", ref], capture_output=True, text=True, check=True).stdout
    # The new commit went to the new remote, not the old one.
    assert first_head != second_head


def test_network_git_operations_get_a_longer_configurable_timeout(monkeypatch):
    monkeypatch.delenv("BINDERY_SYNC_NETWORK_TIMEOUT", raising=False)
    assert sync._network_timeout() >= 300
    monkeypatch.setenv("BINDERY_SYNC_NETWORK_TIMEOUT", "45")
    assert sync._network_timeout() == 45
    monkeypatch.setenv("BINDERY_SYNC_NETWORK_TIMEOUT", "nonsense")
    assert sync._network_timeout() >= 300


def test_timeout_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "child-survived"
    # The shell starts a background child (standing in for ssh) and waits.
    script = f"(sleep 3; touch {marker}) & wait"
    with pytest.raises(subprocess.TimeoutExpired):
        sync._run_killing_group(["/bin/sh", "-c", script], input_data=None, env={"PATH": "/usr/bin:/bin"}, timeout=0.5)
    import time
    time.sleep(3.5)
    assert not marker.exists()
