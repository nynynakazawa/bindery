"""Two agents, one vault, no lost writes.

These tests use real subprocesses rather than threads. The failure they guard
against is a read-modify-write race between two `bindery serve` processes, and
a thread-based imitation of that would exercise a different locking path than
the one that actually runs in production.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")

#: Constructs a server and records one entry, exactly as an agent would.
LEARN_WORKER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from bindery.config import Config
    from bindery.server import MemoryServer

    config = Config.resolve(vault=sys.argv[2], state_dir=sys.argv[3], semantic=False)
    MemoryServer(config).tool_memory_learn({"content": sys.argv[4], "tags": [sys.argv[5]]})
    """
)


def _run_workers(script: str, vault: Path, state: Path, payloads: list[tuple[str, str]]):
    def run(payload: tuple[str, str]):
        return subprocess.run(
            [sys.executable, "-c", script, SRC, str(vault), str(state), *payload],
            capture_output=True,
            text=True,
            timeout=120,
        )

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(run, payloads))


def test_concurrent_learn_keeps_every_entry(tmp_path):
    """The lost update this project cannot afford: one agent's memory vanishing."""
    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "state"

    payloads = [(f"entry-{i:02d}", f"tag{i:02d}") for i in range(8)]
    results = _run_workers(LEARN_WORKER, vault, state, payloads)

    failed = [r for r in results if r.returncode != 0]
    assert not failed, "worker failed:\n" + "\n".join(r.stderr for r in failed)

    journals = list((vault / "journal").glob("*.md"))
    assert len(journals) == 1
    body = journals[0].read_text(encoding="utf-8")

    missing = [content for content, _ in payloads if content not in body]
    assert not missing, f"lost {len(missing)} of {len(payloads)} entries: {missing}"

    # Front matter must survive the merge too, not just the entries.
    for _, tag in payloads:
        assert tag in body


def test_concurrent_learn_leaves_valid_frontmatter(tmp_path):
    """A merge that interleaves badly would produce two front matter blocks."""
    from bindery.indexer import parse_frontmatter

    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "state"

    payloads = [(f"note-{i:02d}", f"t{i:02d}") for i in range(6)]
    _run_workers(LEARN_WORKER, vault, state, payloads)

    journal = next((vault / "journal").glob("*.md"))
    text = journal.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)

    assert meta.get("title", "").startswith("Journal ")
    assert "---" not in body.split("\n")[0]
    assert body.count("# Journal ") == 1


def test_atomic_write_never_leaves_a_partial_file(tmp_path, monkeypatch):
    """A failure mid-write must leave the previous version, not a truncated one."""
    import bindery.safeio as safeio

    target = tmp_path / "note.md"
    target.write_text("original\n", encoding="utf-8")

    class Boom(Exception):
        pass

    def explode(src, dst):
        raise Boom

    monkeypatch.setattr(safeio.os, "replace", explode)
    with pytest.raises(Boom):
        safeio.atomic_write(target, "replacement" * 1000)

    assert target.read_text(encoding="utf-8") == "original\n"
    # and no temporary file left behind
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]


def test_write_is_visible_all_at_once(tmp_path):
    """A reader must never observe a half-written note."""
    import threading

    from bindery.safeio import update_text

    target = tmp_path / "note.md"
    state = tmp_path / "state"
    big = "line\n" * 20000
    target.write_text(big, encoding="utf-8")

    seen: list[int] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                seen.append(len(target.read_text(encoding="utf-8")))
            except FileNotFoundError:  # pragma: no cover - never with rename
                seen.append(-1)

    watcher = threading.Thread(target=reader)
    watcher.start()
    try:
        for _ in range(20):
            update_text(target, lambda _c: big, lock_dir=state)
    finally:
        stop.set()
        watcher.join()

    # Every observation is a complete file - one length, never a partial one.
    assert set(seen) == {len(big)}


def test_update_text_serialises_readers_and_writers(tmp_path):
    """The lock must cover the read, not just the write."""
    from bindery.safeio import update_text

    target = tmp_path / "counter.md"
    state = tmp_path / "state"

    def bump(current: str) -> str:
        return current + "x"

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: update_text(target, bump, lock_dir=state), range(40)))

    assert target.read_text(encoding="utf-8") == "x" * 40


def test_lock_timeout_is_reported_not_swallowed(tmp_path):
    """Silently skipping a write would be worse than failing."""
    from bindery.safeio import LockTimeout, file_lock

    target = tmp_path / "note.md"
    state = tmp_path / "state"

    with file_lock(target, state):
        with pytest.raises(LockTimeout):
            with file_lock(target, state, timeout=0.1):
                pass  # pragma: no cover


def test_lock_files_stay_out_of_the_vault(tmp_path):
    """The vault must remain a clean set of Markdown files."""
    from bindery.safeio import update_text

    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "state"

    update_text(vault / "note.md", lambda _: "hello\n", lock_dir=state)

    assert [p.name for p in vault.iterdir()] == ["note.md"]
    assert list((state / "locks").glob("*.lock"))
