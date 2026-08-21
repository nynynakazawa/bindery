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

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    title    TEXT NOT NULL DEFAULT '',
    tags     TEXT NOT NULL DEFAULT '[]',
    mtime    REAL NOT NULL DEFAULT 0,
    digest   TEXT NOT NULL DEFAULT '',
    -- Which codebase this note is about; '' means it spans all of them.
    project  TEXT NOT NULL DEFAULT '',
    -- durable | journal | episode. See indexer.TIER_PRIOR.
    tier     TEXT NOT NULL DEFAULT 'durable'
);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    note_id  INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    -- Full heading trail, e.g. 'Auth / Backend / Refresh token'.
    breadcrumb TEXT NOT NULL DEFAULT '',
    body     TEXT NOT NULL,
    tokens   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_note ON chunks(note_id);

-- Trigram tokenizer: required for CJK substring matching.
--
-- Title and tags are indexed alongside the passage because in a personal
-- knowledge base the note's name is often the most precise statement of what
-- it is about, and the body may never repeat it. Searching only the body
-- misses "the note called exactly this".
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title,
    tags,
    breadcrumb,
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
-- `impressions` counts appearing in a result list; `uses` counts being read
-- afterwards. Conflating them is how a retrieval system teaches itself that
-- whatever it already ranks highly is what people want.
CREATE TABLE IF NOT EXISTS usage (
    path        TEXT PRIMARY KEY,
    uses        INTEGER NOT NULL DEFAULT 0,
    impressions INTEGER NOT NULL DEFAULT 0,
    last_used   REAL NOT NULL DEFAULT 0,
    last_shown  REAL NOT NULL DEFAULT 0,
    pinned      INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(slots=True)
class Chunk:
    """One retrievable passage."""

    chunk_id: int
    note_id: int
    path: str
    title: str
    breadcrumb: str
    body: str
    tokens: int
    project: str = ""
    tier: str = "durable"


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
        # check_same_thread=False because the MCP SDK dispatches synchronous
        # tools onto a worker thread, and not always the same one. Nothing here
        # is thread-safe on its own, so MemoryServer serialises every tool call
        # behind one lock - that, not this flag, is what makes it safe.
        self.conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # busy_timeout FIRST. Switching journal modes needs a brief exclusive
        # lock, and with several agents starting at once one of them will find
        # the database held by another. Set after journal_mode, the timeout is
        # still zero at the moment it is needed, and the loser crashes on
        # startup with "database is locked" rather than waiting a few
        # milliseconds for its turn.
        self.conn.execute("PRAGMA busy_timeout=15000")
        self._enable_wal()
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._depth = 0
        self._vec_dim: int | None = None
        self._vec = self._load_vector_extension()
        self._pending_rebuild = False
        self._migrate()
        # Outside a transaction on purpose: sqlite3.executescript() issues an
        # implicit COMMIT before it runs, which would silently close one we
        # had opened. Every statement is CREATE ... IF NOT EXISTS, so two
        # processes starting at once resolve through the busy timeout.
        self.conn.executescript(_SCHEMA)
        with self.write():
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            if self._pending_rebuild:
                self.set_meta("rebuild_required", "1")

    #: Columns added to `usage` after it shipped. It is the one table a schema
    #: change must not drop - retrieval history exists nowhere else and cannot
    #: be rebuilt from the vault - so it is migrated in place instead.
    _USAGE_COLUMNS = {
        "impressions": "INTEGER NOT NULL DEFAULT 0",
        "last_shown": "REAL NOT NULL DEFAULT 0",
    }

    def _add_missing_columns(self) -> None:
        try:
            existing = {
                row[1] for row in self.conn.execute("PRAGMA table_info(usage)")
            }
        except sqlite3.DatabaseError:
            return
        if not existing:
            return
        for column, spec in self._USAGE_COLUMNS.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE usage ADD COLUMN {column} {spec}")

    def _load_vector_extension(self) -> bool:
        """Load sqlite-vec if it is installed and this build allows it.

        Optional twice over: the package ships with the semantic extra, and
        loading extensions is compiled out of some Python builds. Neither is
        an error - brute force over the same vectors still answers the same
        query, just linearly.
        """
        try:
            import sqlite_vec  # type: ignore[import-not-found]
        except ImportError:
            return False
        try:
            self.conn.enable_load_extension(True)
        except (AttributeError, sqlite3.OperationalError):
            return False
        try:
            sqlite_vec.load(self.conn)
            return True
        except Exception:
            return False
        finally:
            try:
                self.conn.enable_load_extension(False)
            except (AttributeError, sqlite3.OperationalError):
                pass

    def _ensure_vec_table(self, dim: int) -> bool:
        """Create the ANN index on first use, once the dimension is known.

        The dimension is a property of whichever embedding backend is
        installed, so it cannot be part of the static schema. A mismatch means
        the backend changed; the index is rebuilt rather than reconciled,
        since it is derived from vectors this database already holds.
        """
        if not self._vec:
            return False
        if self._vec_dim == dim:
            return True
        try:
            stored = self.get_meta("vector_dim")
            if stored and int(stored) != dim:
                self.conn.execute("DROP TABLE IF EXISTS vec_chunks")
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING "
                f"vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{dim}])"
            )
            self.set_meta("vector_dim", str(dim))
        except sqlite3.DatabaseError:
            self._vec = False
            return False
        self._vec_dim = dim
        return True

    @property
    def ann_enabled(self) -> bool:
        return bool(self._vec)

    def _enable_wal(self) -> None:
        """Turn on WAL, tolerating the race between simultaneous first opens.

        WAL is what lets Claude Code and Codex hold the same index open at
        once, so this is worth retrying rather than failing on. Some SQLite
        builds bypass the busy handler for this pragma entirely, which is why
        the belt-and-braces loop exists on top of the timeout.
        """
        import time

        for attempt in range(10):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _migrate(self) -> None:
        """Drop derived tables when their shape changed, keeping what is learned.

        Everything describing the notes is rebuilt from Markdown on the next
        scan, so an old layout is discarded rather than migrated - there is
        nothing in it that the vault does not already say. The growth tables
        are the exception: retrieval history exists nowhere else, and it is
        keyed by note path, which no schema change here invalidates.
        """
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return
        if row is None or str(row[0]) == str(SCHEMA_VERSION):
            return
        self._add_missing_columns()
        # Recorded so that the next `status` can say "an upgrade reset this,
        # run bindery index" rather than "your vault appears to be empty",
        # which is what a user sees at the worst possible moment otherwise.
        self._pending_rebuild = True
        for statement in (
            "DROP TABLE IF EXISTS chunks_fts",
            "DROP TABLE IF EXISTS vectors",
            "DROP TABLE IF EXISTS links",
            "DROP TABLE IF EXISTS chunks",
            "DROP TABLE IF EXISTS notes",
        ):
            self.conn.execute(statement)

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

    @property
    def rebuild_required(self) -> bool:
        return self.get_meta("rebuild_required") == "1"

    def clear_rebuild_flag(self) -> None:
        with self.write():
            self.conn.execute("DELETE FROM meta WHERE key='rebuild_required'")

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # --------------------------------------------------------------- notes

    def note_digest(self, path: str) -> str | None:
        row = self.conn.execute("SELECT digest FROM notes WHERE path=?", (path,)).fetchone()
        return row["digest"] if row else None

    def note_fingerprints(self) -> dict[str, tuple[float, str]]:
        """Every indexed note's ``(mtime, digest)``, fetched in one query."""
        return {
            r["path"]: (float(r["mtime"]), r["digest"])
            for r in self.conn.execute("SELECT path, mtime, digest FROM notes")
        }

    def touch_note(self, path: str, mtime: float) -> None:
        """Record a new mtime for a note whose contents did not change.

        Without this, a file that is rewritten with identical contents fails
        the mtime check on every future scan and is read and hashed forever.
        """
        with self.write():
            self.conn.execute("UPDATE notes SET mtime=? WHERE path=?", (mtime, path))

    def chunk_ids_for(self, path: str) -> list[int]:
        return [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT c.id FROM chunks c JOIN notes n ON n.id = c.note_id "
                "WHERE n.path=? ORDER BY c.seq",
                (path,),
            )
        ]

    def chunks_missing_vectors(self, chunk_ids: list[int] | None = None):
        if chunk_ids is None:
            return self.iter_chunks_without_vectors()
        if not chunk_ids:
            return []
        marks = ",".join("?" * len(chunk_ids))
        return self.conn.execute(
            f"SELECT c.id, c.breadcrumb, c.body FROM chunks c "
            f"LEFT JOIN vectors v ON v.chunk_id = c.id "
            f"WHERE v.chunk_id IS NULL AND c.id IN ({marks})",
            chunk_ids,
        ).fetchall()

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
        if self._vec and self._vec_dim is not None:
            self.conn.execute(
                "DELETE FROM vec_chunks WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE note_id=?)",
                (note_id,),
            )
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
        project: str = "",
        tier: str = "durable",
    ) -> int:
        """Replace a note and all of its derived rows atomically."""
        with self.write():
            return self._upsert_note_locked(
                path=path, title=title, tags=tags, mtime=mtime,
                digest=digest, chunks=chunks, links=links,
                project=project, tier=tier,
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
        project: str = "",
        tier: str = "durable",
    ) -> int:
        self.delete_note(path)
        cur = self.conn.execute(
            "INSERT INTO notes(path, title, tags, mtime, digest, project, tier) "
            "VALUES(?,?,?,?,?,?,?)",
            (path, title, json.dumps(tags, ensure_ascii=False), mtime, digest, project, tier),
        )
        note_id = int(cur.lastrowid)
        for seq, (breadcrumb, body, tokens) in enumerate(chunks):
            cur = self.conn.execute(
                "INSERT INTO chunks(note_id, seq, breadcrumb, body, tokens) VALUES(?,?,?,?,?)",
                (note_id, seq, breadcrumb, body, tokens),
            )
            chunk_id = int(cur.lastrowid)
            self.conn.execute(
                "INSERT INTO chunks_fts(rowid, title, tags, breadcrumb, body) "
                "VALUES(?,?,?,?,?)",
                (chunk_id, title, " ".join(tags), breadcrumb, body),
            )
        for target in links:
            self.conn.execute("INSERT INTO links(src_note_id, target) VALUES(?,?)", (note_id, target))
        return note_id

    # --------------------------------------------------------------- scope

    def scope_clause(self, scope: str, project: str) -> tuple[str, list[str]]:
        """SQL restricting a ranking to one project's memory.

        Filtering here rather than after ranking is deliberate: taking the top
        N and then discarding other projects' rows would return fewer results
        the more crowded the vault gets, which is the opposite of what the
        caller asked for.

        Unscoped notes are included alongside the current project because a
        note that is not about any one codebase - a language idiom, a workflow
        preference - is exactly the knowledge worth carrying between them.
        """
        if scope == "all" or not project:
            return "", []
        if scope == "global":
            return "AND notes.project = ''", []
        return "AND (notes.project = ? OR notes.project = '')", [project]

    # -------------------------------------------------------------- chunks

    def chunk_rows(self, chunk_ids: list[int]) -> dict[int, Chunk]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"""SELECT c.id, c.note_id, c.breadcrumb, c.body, c.tokens,
                       n.path, n.title, n.project, n.tier
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
                breadcrumb=r["breadcrumb"],
                body=r["body"],
                tokens=r["tokens"],
                project=r["project"],
                tier=r["tier"],
            )
            for r in rows
        }

    def iter_chunks_without_vectors(self):
        return self.conn.execute(
            "SELECT c.id, c.breadcrumb, c.body FROM chunks c "
            "LEFT JOIN vectors v ON v.chunk_id = c.id WHERE v.chunk_id IS NULL"
        ).fetchall()

    def store_vector(self, chunk_id: int, values: list[float]) -> None:
        blob = pack_vector(values)
        self.conn.execute(
            "INSERT INTO vectors(chunk_id, dim, vec) VALUES(?,?,?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
            (chunk_id, len(values), blob),
        )
        # `vectors` stays the canonical copy: the ANN index is derived from it
        # and can be dropped and rebuilt, which is what makes switching
        # embedding backends - or losing the extension - a non-event.
        if self._ensure_vec_table(len(values)):
            try:
                self.conn.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (chunk_id,))
                self.conn.execute(
                    "INSERT INTO vec_chunks(chunk_id, embedding) VALUES(?,?)",
                    (chunk_id, blob),
                )
            except sqlite3.DatabaseError:
                self._vec = False

    def nearest_vectors(self, query: list[float], limit: int) -> list[int] | None:
        """Approximate nearest neighbours, or ``None`` if unavailable.

        Returning ``None`` rather than an empty list matters: no index and no
        matches are different answers, and the caller falls back to the exact
        scan only for the first.
        """
        if not self._ensure_vec_table(len(query)):
            return None
        try:
            rows = self.conn.execute(
                "SELECT chunk_id FROM vec_chunks WHERE embedding MATCH ? "
                "AND k = ? ORDER BY distance",
                (pack_vector(query), limit),
            ).fetchall()
        except sqlite3.DatabaseError:
            return None
        return [int(r["chunk_id"]) for r in rows]

    def rebuild_vector_index(self) -> int:
        """Populate the ANN index from the vectors already stored."""
        rows = self.conn.execute("SELECT chunk_id, dim, vec FROM vectors").fetchall()
        if not rows or not self._ensure_vec_table(int(rows[0]["dim"])):
            return 0
        done = 0
        with self.write():
            self.conn.execute("DELETE FROM vec_chunks")
            for row in rows:
                self.conn.execute(
                    "INSERT INTO vec_chunks(chunk_id, embedding) VALUES(?,?)",
                    (int(row["chunk_id"]), row["vec"]),
                )
                done += 1
        return done

    def all_vectors(self, scope_sql: str = "", scope_params: list | None = None):
        return self.conn.execute(
            "SELECT v.chunk_id, v.dim, v.vec FROM vectors v "
            "JOIN chunks ON chunks.id = v.chunk_id "
            "JOIN notes ON notes.id = chunks.note_id "
            f"WHERE 1=1 {scope_sql}",
            scope_params or [],
        ).fetchall()

    def filter_chunks_in_scope(
        self, chunk_ids: list[int], scope_sql: str, scope_params: list | None = None
    ) -> list[int]:
        """Keep only the ids inside the scope, preserving the given order."""
        if not chunk_ids or not scope_sql:
            return chunk_ids
        marks = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"""SELECT chunks.id FROM chunks
                JOIN notes ON notes.id = chunks.note_id
                WHERE chunks.id IN ({marks}) {scope_sql}""",
            [*chunk_ids, *(scope_params or [])],
        ).fetchall()
        allowed = {int(r["id"]) for r in rows}
        return [chunk_id for chunk_id in chunk_ids if chunk_id in allowed]

    def projects(self) -> list[tuple[str, int]]:
        """Every project the vault knows about, with note counts."""
        return [
            (r["project"], int(r["n"]))
            for r in self.conn.execute(
                "SELECT project, COUNT(*) AS n FROM notes GROUP BY project ORDER BY n DESC"
            )
        ]

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

    def record_impressions(self, paths: list[str]) -> None:
        """Note that these passages were *shown*. Not that they helped.

        This used to be counted as usage and fed straight back into ranking,
        which is a feedback loop with no ground truth in it: a note that ranks
        third gets shown, shown becomes a point, points raise the rank, and the
        note keeps being shown. Whether it ever answered anything never enters
        into it. Impressions are still worth recording - they are what makes
        "shown constantly, never opened" a detectable state - but they do not
        move the ranking.
        """
        import time

        now = time.time()
        with self.write():
            for path in paths:
                self.conn.execute(
                    "INSERT INTO usage(path, impressions, last_shown) VALUES(?,1,?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "impressions = impressions + 1, last_shown = ?",
                    (path, now, now),
                )

    def record_use(self, paths: list[str]) -> None:
        """Count a passage that an agent actually went on to read in full.

        Following a search result to the note behind it is the closest thing
        to a statement of usefulness this system can observe without asking
        anyone. It is the only signal that raises a note's ranking.
        """
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

    def shown_but_unread(self, *, minimum: int = 5, limit: int = 10):
        """Notes that keep turning up in results and are never opened.

        Either they answer the question in the passage alone - fine - or they
        are matching queries they have nothing to say about, which is the
        failure the old feedback loop would have entrenched rather than
        surfaced.
        """
        return [
            (r["path"], int(r["impressions"]))
            for r in self.conn.execute(
                "SELECT path, impressions FROM usage "
                "WHERE uses = 0 AND pinned = 0 AND impressions >= ? "
                "ORDER BY impressions DESC LIMIT ?",
                (minimum, limit),
            )
        ]

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
