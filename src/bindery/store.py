"""SQLite-backed index.

The vault's Markdown files remain the source of truth. Everything here is a
derived index that can be deleted and rebuilt at any time, which keeps the
notes portable and greppable rather than locked inside a database.

The full-text index uses FTS5's ``trigram`` tokenizer. That choice is what
makes Japanese work without a morphological analyser: the default ``unicode61``
tokenizer splits on whitespace and therefore cannot index a Japanese sentence
at all, while trigram indexes overlapping 3-character sequences and matches
substrings in any script.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    title    TEXT NOT NULL DEFAULT '',
    tags     TEXT NOT NULL DEFAULT '[]',
    mtime    REAL NOT NULL DEFAULT 0,
    digest   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    note_id  INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    heading  TEXT NOT NULL DEFAULT '',
    body     TEXT NOT NULL,
    tokens   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_note ON chunks(note_id);

-- Trigram tokenizer: required for CJK substring matching.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    heading,
    body,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS links (
    src_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS links_src ON links(src_note_id);
CREATE INDEX IF NOT EXISTS links_target ON links(target);

CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- growth --
-- Retrieval telemetry. This is the substrate the memory grows on: what the
-- agents actually looked for, and what actually answered them. It is derived
-- data like everything else here, so deleting it costs nothing but the
-- learned ranking.

CREATE TABLE IF NOT EXISTS queries (
    id       INTEGER PRIMARY KEY,
    text     TEXT NOT NULL,
    ts       REAL NOT NULL,
    hits     INTEGER NOT NULL DEFAULT 0,
    agent    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS queries_text ON queries(text);
CREATE INDEX IF NOT EXISTS queries_ts ON queries(ts);

-- Usage is keyed by note path rather than chunk id, because chunk ids are
-- rebuilt on every reindex while a note's path is stable. Keying on chunk id
-- would silently reset everything the system had learned each time a file was
-- edited.
CREATE TABLE IF NOT EXISTS usage (
    path      TEXT PRIMARY KEY,
    uses      INTEGER NOT NULL DEFAULT 0,
    last_used REAL NOT NULL DEFAULT 0,
    pinned    INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(slots=True)
class Chunk:
    """One retrievable passage."""

    chunk_id: int
    note_id: int
    path: str
    title: str
    heading: str
    body: str
    tokens: int


def pack_vector(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


class Store:
    """Thin persistence layer over SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None turns off the driver's implicit DEFERRED
        # transactions so that writes can be wrapped in BEGIN IMMEDIATE
        # instead. The distinction matters with two agents running: a deferred
        # transaction takes its write lock late, after it has already read, and
        # if the other process wrote in between there is no way to resolve it -
        # SQLite returns SQLITE_BUSY immediately and the busy timeout does not
        # apply, because waiting could only deadlock. IMMEDIATE takes the lock
        # up front, so the second writer waits its turn and then proceeds.
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # WAL plus a busy timeout is what lets Claude Code and Codex hold the
        # same index open at once without either of them erroring out.
        self.conn.execute("PRAGMA busy_timeout=15000")
        self._depth = 0
        # Outside a transaction on purpose: sqlite3.executescript() issues an
        # implicit COMMIT before it runs, which would silently close one we
        # had opened. Every statement is CREATE ... IF NOT EXISTS, so two
        # processes starting at once resolve through the busy timeout.
        self.conn.executescript(_SCHEMA)
        with self.write():
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -------------------------------------------------------- transactions

    @contextmanager
    def write(self) -> Iterator[None]:
        """Run a write as one serialised, all-or-nothing transaction.

        Re-entrant, so a caller that already opened a transaction can call
        methods that would otherwise open their own without nesting BEGINs -
        SQLite has no nested transactions, and the inner COMMIT would publish
        the outer one's half-finished work.

        Scope is deliberately one note rather than a whole reindex: the write
        lock is exclusive, and holding it for an entire vault scan would stall
        the other agent for as long as the scan takes.
        """
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self.conn.execute("ROLLBACK")
            raise
        self._depth = 0
        self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # --------------------------------------------------------------- notes

    def note_digest(self, path: str) -> str | None:
        row = self.conn.execute("SELECT digest FROM notes WHERE path=?", (path,)).fetchone()
        return row["digest"] if row else None

    def all_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM notes")}

    def delete_note(self, path: str) -> None:
        with self.write():
            row = self.conn.execute("SELECT id FROM notes WHERE path=?", (path,)).fetchone()
            if row is None:
                return
            self._drop_chunk_rows(row["id"])
            self.conn.execute("DELETE FROM notes WHERE id=?", (row["id"],))

    def _drop_chunk_rows(self, note_id: int) -> None:
        """Remove a note's chunks, keeping the external FTS table in step.

        chunks_fts is an ordinary (non-contentless) FTS5 table whose rowids are
        kept equal to chunks.id, so deletions have to be mirrored explicitly.
        """
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM chunks WHERE note_id=?", (note_id,))]
        for chunk_id in ids:
            self.conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (chunk_id,))
        self.conn.execute("DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE note_id=?)", (note_id,))
        self.conn.execute("DELETE FROM chunks WHERE note_id=?", (note_id,))

    def upsert_note(
        self,
        *,
        path: str,
        title: str,
        tags: list[str],
        mtime: float,
        digest: str,
        chunks: list[tuple[str, str, int]],
        links: list[str],
    ) -> int:
        """Replace a note and all of its derived rows atomically."""
        with self.write():
            return self._upsert_note_locked(
                path=path, title=title, tags=tags, mtime=mtime,
                digest=digest, chunks=chunks, links=links,
            )

    def _upsert_note_locked(
        self,
        *,
        path: str,
        title: str,
        tags: list[str],
        mtime: float,
        digest: str,
        chunks: list[tuple[str, str, int]],
        links: list[str],
    ) -> int:
        self.delete_note(path)
        cur = self.conn.execute(
            "INSERT INTO notes(path, title, tags, mtime, digest) VALUES(?,?,?,?,?)",
            (path, title, json.dumps(tags, ensure_ascii=False), mtime, digest),
        )
        note_id = int(cur.lastrowid)
        for seq, (heading, body, tokens) in enumerate(chunks):
            cur = self.conn.execute(
                "INSERT INTO chunks(note_id, seq, heading, body, tokens) VALUES(?,?,?,?,?)",
                (note_id, seq, heading, body, tokens),
            )
            chunk_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO chunks_fts(rowid, heading, body) VALUES(?,?,?)",
                (chunk_id, heading, body),
            )
        for target in links:
            self.conn.execute("INSERT INTO links(src_note_id, target) VALUES(?,?)", (note_id, target))
        return note_id

    # -------------------------------------------------------------- chunks

    def chunk_rows(self, chunk_ids: list[int]) -> dict[int, Chunk]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"""SELECT c.id, c.note_id, c.heading, c.body, c.tokens, n.path, n.title
                FROM chunks c JOIN notes n ON n.id = c.note_id
                WHERE c.id IN ({marks})""",
            chunk_ids,
        ).fetchall()
        return {
            r["id"]: Chunk(
                chunk_id=r["id"],
                note_id=r["note_id"],
                path=r["path"],
                title=r["title"],
                heading=r["heading"],
                body=r["body"],
                tokens=r["tokens"],
            )
            for r in rows
        }

    def iter_chunks_without_vectors(self):
        return self.conn.execute(
            "SELECT c.id, c.heading, c.body FROM chunks c "
            "LEFT JOIN vectors v ON v.chunk_id = c.id WHERE v.chunk_id IS NULL"
        ).fetchall()

    def store_vector(self, chunk_id: int, values: list[float]) -> None:
        self.conn.execute(
            "INSERT INTO vectors(chunk_id, dim, vec) VALUES(?,?,?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
            (chunk_id, len(values), pack_vector(values)),
        )

    def all_vectors(self):
        return self.conn.execute("SELECT chunk_id, dim, vec FROM vectors").fetchall()

    # --------------------------------------------------------------- stats

    def stats(self) -> dict[str, int]:
        one = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "notes": one("SELECT COUNT(*) FROM notes"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "links": one("SELECT COUNT(*) FROM links"),
            "vectors": one("SELECT COUNT(*) FROM vectors"),
        }

    # -------------------------------------------------------------- growth

    def record_query(self, text: str, hits: int, agent: str = "") -> None:
        import time

        with self.write():
            self.conn.execute(
                "INSERT INTO queries(text, ts, hits, agent) VALUES(?,?,?,?)",
                (text, time.time(), hits, agent),
            )

    def record_use(self, paths: list[str]) -> None:
        """Count a retrieval against every note that supplied a passage."""
        import time

        now = time.time()
        with self.write():
            for path in paths:
                self.conn.execute(
                    "INSERT INTO usage(path, uses, last_used) VALUES(?,1,?) "
                    "ON CONFLICT(path) DO UPDATE SET uses = uses + 1, last_used = ?",
                    (path, now, now),
                )

    def usage_map(self) -> dict[str, tuple[int, float, int]]:
        return {
            r["path"]: (int(r["uses"]), float(r["last_used"]), int(r["pinned"]))
            for r in self.conn.execute("SELECT path, uses, last_used, pinned FROM usage")
        }

    def set_pinned(self, path: str, pinned: bool) -> None:
        with self.write():
            self.conn.execute(
                "INSERT INTO usage(path, uses, last_used, pinned) VALUES(?,0,0,?) "
                "ON CONFLICT(path) DO UPDATE SET pinned=excluded.pinned",
                (path, int(pinned)),
            )

    def prune_queries(self, keep: int = 5000) -> int:
        """Cap the telemetry table so it cannot grow without bound."""
        with self.write():
            row = self.conn.execute("SELECT COUNT(*) FROM queries").fetchone()
            total = int(row[0])
            if total <= keep:
                return 0
            self.conn.execute(
                "DELETE FROM queries WHERE id NOT IN "
                "(SELECT id FROM queries ORDER BY ts DESC LIMIT ?)",
                (keep,),
            )
        return total - keep

    def commit(self) -> None:
        """Retained for callers; writes already committed themselves.

        Every mutating method wraps itself in :meth:`write`, so there is never
        an open transaction to flush here. Kept as a no-op rather than removed
        because "commit at the end" is what callers reasonably expect to exist.
        """

    def close(self) -> None:
        self.conn.close()
