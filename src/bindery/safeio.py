"""Crash-safe, multi-process-safe writes to the vault.

Two agents mean two ``bindery serve`` processes, and the interesting race is
not in SQLite - WAL and a busy timeout already handle that - but in the
Markdown files, which have no transactions at all. ``memory_learn`` appends to
one journal file per day, so both agents read the same file, add their own
entry, and write the whole thing back:

    Claude                          Codex
    read journal -> A
                                    read journal -> A
    write A + claude's entry
                                    write A + codex's entry   <- Claude's entry is gone

Nothing errors. The file stays valid Markdown. One agent's memory simply never
existed. That is the worst failure mode this project can have, because the
whole premise is that what one agent writes, the other one reads.

Two mechanisms fix it, and both are needed:

*Locking* serialises the read-modify-write, so the second writer sees the first
writer's entry before adding its own.

*Atomic replacement* means a write is never partially visible. ``write_text``
truncates the file and then writes into it, so a crash - or a reader arriving
mid-write - sees a half-written note. Writing to a temporary file and renaming
it over the target makes the switch a single filesystem operation: readers see
either the whole old file or the whole new one, never a mixture. This is also
why readers do not need the lock.

Lock files live in the state directory rather than beside the notes, so the
vault stays a clean set of Markdown files that Obsidian and git can own.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

#: How long to wait for another process to finish its update before giving up.
#: Generous, because the critical section is a read and a write of one small
#: file - if it takes longer than this, something is wrong and failing loudly
#: beats corrupting the note.
LOCK_TIMEOUT = 10.0

#: Retry interval while the lock is held elsewhere.
LOCK_POLL = 0.02


class LockTimeout(RuntimeError):
    """Another process held the lock for longer than the caller would wait."""


def lock_file_for(target: Path, lock_dir: Path) -> Path:
    """Where the lock for ``target`` lives.

    Keyed by the resolved path so that two processes reaching the same note by
    different routes - a symlinked vault, a relative path - still contend for
    the same lock.
    """
    key = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:16]
    return lock_dir / "locks" / f"{key}.lock"


def _acquire(handle, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not lock within {timeout}s")
            time.sleep(LOCK_POLL)


def _release(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def file_lock(target: Path, lock_dir: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold an exclusive advisory lock covering ``target``.

    Advisory, so it only excludes other Bindery processes - an external editor
    writing the same file at the same moment is out of scope, and no lock a
    user-space program can take would stop it anyway.
    """
    lock = lock_file_for(target, lock_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock, "a+b")
    try:
        _acquire(handle, timeout)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself, not just the bytes it points at."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - not supported everywhere
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - directory fsync is a no-op on some filesystems
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` in a single, all-or-nothing step.

    The temporary file is created in the destination directory because
    ``os.replace`` is only atomic within one filesystem.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(directory)


def update_text(
    path: Path,
    transform: Callable[[str], str],
    *,
    lock_dir: Path,
    timeout: float = LOCK_TIMEOUT,
) -> str:
    """Read, transform, and write ``path`` with nobody else in between.

    ``transform`` receives the current contents (empty string if the file does
    not exist) and returns the full new contents. It runs while the lock is
    held, so it must not block on anything slow.
    """
    with file_lock(path, lock_dir, timeout):
        current = ""
        if path.exists():
            current = path.read_text(encoding="utf-8")
        updated = transform(current)
        atomic_write(path, updated)
    return updated
