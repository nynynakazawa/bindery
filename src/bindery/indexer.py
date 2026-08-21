"""Markdown scanning, chunking, and incremental indexing.

Chunking is where the token savings come from. A retrieval system that returns
whole notes will happily hand an agent a 4,000-token document because one
sentence matched. Splitting on headings and returning the matching section
instead is the difference between a memory layer that pays for itself and one
that costs more than it saves.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .store import Store
from .tokens import estimate_tokens

#: Obsidian-style wiki links. The user's existing notes already use these, so
#: the graph comes for free without asking anyone to add metadata.
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

#: Directories that never contain knowledge worth indexing.
SKIP_DIRS = {".git", ".obsidian", ".trash", "node_modules", "__pycache__", ".bindery"}

#: Path prefixes that are written into the vault for people to read but must
#: never be retrievable.
#:
#: The session records under ``journal/sessions/`` are the reason this exists,
#: and the reason is worth stating plainly: those records list the questions a
#: session could NOT answer. Index them and the record of a missing answer
#: starts matching the very query it was written about - so the second time
#: anyone asks, the gap looks answered, and it silently disappears from
#: ``memory_review``. A note saying "nobody knew X" must never be retrievable
#: as an answer about X. Gaps are tracked in the queries table instead, which
#: is unaffected by anything written into the vault.
SKIP_PREFIXES = ("journal/sessions/",)


def _under(rel: str, prefix: str) -> bool:
    """Is ``rel`` the file ``prefix`` or something inside the directory ``prefix``?

    Prefix comparison on raw strings would make ``private`` also match
    ``private-ish-notes.md``, which is the wrong way for a privacy boundary to
    fail.
    """
    rel = rel.replace("\\", "/").strip("/")
    prefix = prefix.replace("\\", "/").strip("/")
    return rel == prefix or rel.startswith(prefix + "/")


def is_indexable(rel: str, *, include=(), exclude=()) -> bool:
    """Whether one note is inside the boundary the user drew.

    Pointing Bindery at an existing Obsidian vault is the documented happy
    path, and vaults hold more than engineering notes - a diary, client work,
    anything a person keeps. Everything indexed here can come back from
    ``memory_search`` and land in an agent's context, which for a hosted model
    means leaving the machine. So the boundary is explicit and enforced in one
    place, and ``include`` is an allowlist: naming any directory means nothing
    outside it is ever read.
    """
    rel = rel.replace("\\", "/")
    if rel.startswith(SKIP_PREFIXES):
        return False
    if any(_under(rel, prefix) for prefix in exclude):
        return False
    if include:
        return any(_under(rel, prefix) for prefix in include)
    return True


def project_of(rel: str, meta: dict[str, str] | None = None, include=()) -> str:
    """Which codebase a note belongs to, or "" for knowledge that spans them.

    Front matter wins, because a note can then be moved without changing what
    it is about. The directory is the fallback, which makes the convention of
    one folder per project work with no metadata at all - and a note sitting
    loose at the vault root is genuinely general, so it stays unscoped.

    ``include`` shifts where the search for a project name starts. A vault
    organised as ``work/alpha/`` and ``work/beta/`` would otherwise report one
    project called "work", but naming ``work`` as the indexed directory has
    already said that it is a container rather than a project.
    """
    if meta:
        declared = str(meta.get("project", "")).strip()
        if declared:
            return declared
    rel = rel.replace("\\", "/")
    for prefix in sorted(include, key=len, reverse=True):
        prefix = prefix.replace("\\", "/").strip("/")
        if prefix and rel.startswith(prefix + "/"):
            rel = rel[len(prefix) + 1 :]
            break
    parts = [part for part in rel.split("/") if part]
    if not parts:
        return ""
    if parts[0] == "journal":
        # journal/<project>/<date>.md - a dated file directly under journal/
        # predates project scoping and is left global.
        return parts[1] if len(parts) > 2 else ""
    return parts[0] if len(parts) > 1 else ""


@dataclass(slots=True)
class ParsedNote:
    title: str
    tags: list[str]
    body: str
    links: list[str] = field(default_factory=list)
    project: str = ""


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading YAML front matter block from the body.

    Only the flat ``key: value`` subset is understood, which covers the front
    matter these notes actually use. A full YAML parser would be a dependency
    bought for very little.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            meta: dict[str, str] = {}
            for raw in lines[1:index]:
                if ":" not in raw:
                    continue
                key, _, value = raw.partition(":")
                meta[key.strip()] = value.strip().strip("\"'")
            return meta, "\n".join(lines[index + 1 :]).lstrip("\n")
    return {}, text


def _split_tags(raw: str) -> list[str]:
    cleaned = raw.strip().strip("[]")
    return [part.strip().strip("\"'") for part in cleaned.split(",") if part.strip()]


def parse_note(path: Path, text: str) -> ParsedNote:
    meta, body = parse_frontmatter(text)
    title = meta.get("title") or meta.get("name") or ""
    if not title:
        for line in body.splitlines():
            match = HEADING_RE.match(line)
            if match:
                title = match.group(2).strip()
                break
    if not title:
        title = path.stem
    tags = _split_tags(meta.get("tags", ""))
    links = sorted({m.group(1).strip() for m in WIKILINK_RE.finditer(text)})
    return ParsedNote(
        title=title, tags=tags, body=body, links=links,
        project=str(meta.get("project", "")).strip(),
    )


#: Opens or closes a fenced code block. Backticks or tildes, three or more.
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

#: A table row, or the ---|--- separator under a header row.
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$|^\s*\|?[\s:-]*-{2,}[\s:|-]*$")

#: A list item, including its continuation lines when indented.
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _atomic_runs(lines: list[str]) -> list[list[str]]:
    """Group lines into pieces that must not be split apart.

    Splitting on a line boundary is fine in prose and destructive in
    structure: half a code fence is not code, half a table is not a table, and
    a list item cut from its bullet loses what it was a list of. Each run is
    either one such structure or a single ordinary line.
    """
    runs: list[list[str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            run = [line]
            index += 1
            while index < len(lines):
                run.append(lines[index])
                closing = FENCE_RE.match(lines[index])
                index += 1
                if closing and closing.group(1)[0] == marker:
                    break
            runs.append(run)
            continue
        if TABLE_RE.match(line):
            run = []
            while index < len(lines) and TABLE_RE.match(lines[index]):
                run.append(lines[index])
                index += 1
            runs.append(run)
            continue
        if LIST_RE.match(line):
            run = [line]
            index += 1
            # Continuation lines are indented, or blank between items.
            while index < len(lines) and (
                LIST_RE.match(lines[index])
                or (lines[index].startswith((" ", "\t")) and lines[index].strip())
            ):
                run.append(lines[index])
                index += 1
            runs.append(run)
            continue
        runs.append([line])
        index += 1
    return runs


def _breadcrumb(stack: list[tuple[int, str]]) -> str:
    """``Auth / Backend / Refresh token`` for the current heading stack.

    A chunk labelled only "Refresh token" has lost the thing it is a refresh
    token *for*. The trail is what makes a deep section legible on its own,
    both to the ranking and to whoever reads the passage.
    """
    return " / ".join(title for _level, title in stack if title)


def chunk_markdown(body: str, *, max_tokens: int, overlap: int) -> list[tuple[str, str, int]]:
    """Split ``body`` into ``(breadcrumb, text, tokens)`` passages.

    Sections are cut at Markdown headings first, because a heading is the
    author's own statement about where one idea ends and the next begins.
    Oversized sections are split further, but only between structures - never
    through a code block, a table, or a list item.
    """
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    in_fence = ""
    for line in body.splitlines():
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = marker
            elif marker == in_fence:
                in_fence = ""
        # A "# comment" inside a shell block is not a heading.
        match = None if in_fence else HEADING_RE.match(line)
        if match:
            if current:
                sections.append((_breadcrumb(stack), current))
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            current = []
        else:
            current.append(line)
    if current:
        sections.append((_breadcrumb(stack), current))

    chunks: list[tuple[str, str, int]] = []
    for heading, lines in sections:
        buffer: list[str] = []
        used = 0
        for run in _atomic_runs(lines):
            cost = sum(estimate_tokens(line) for line in run)
            if buffer and used + cost > max_tokens:
                text = "\n".join(buffer).strip()
                if text:
                    chunks.append((heading, text, used))
                tail: list[str] = []
                kept = 0
                for previous in reversed(buffer):
                    previous_cost = estimate_tokens(previous)
                    if kept + previous_cost > overlap:
                        break
                    tail.insert(0, previous)
                    kept += previous_cost
                buffer = [*tail, *run]
                used = kept + cost
            else:
                buffer.extend(run)
                used += cost
        text = "\n".join(buffer).strip()
        if text:
            chunks.append((heading, text, used))
    return chunks


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def iter_markdown(config: Config):
    for path in sorted(config.vault.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = str(path.relative_to(config.vault))
        if not is_indexable(rel, include=config.include, exclude=config.exclude):
            continue
        yield path


@dataclass(slots=True)
class IndexReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    scanned: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "unchanged": self.unchanged,
        }


def _write_note(config: Config, store: Store, path: Path, rel: str, text: str, digest: str) -> None:
    note = parse_note(path, text)
    chunks = chunk_markdown(
        note.body,
        max_tokens=config.chunk_tokens,
        overlap=config.chunk_overlap,
    )
    if not chunks:
        chunks = [("", note.title, estimate_tokens(note.title))]
    store.upsert_note(
        path=rel,
        title=note.title,
        tags=note.tags,
        project=project_of(rel, {"project": note.project}, include=config.include),
        mtime=path.stat().st_mtime,
        digest=digest,
        chunks=chunks,
        links=note.links,
    )


def refresh_embeddings(config: Config, store: Store, chunk_ids: list[int] | None = None) -> int:
    """Embed passages that have no vector yet, newest write first.

    Reindexing a note drops its old chunks, and their vectors go with them.
    Nothing used to put vectors back except an explicit `bindery index
    --embed`, so semantic coverage decayed exactly as the memory grew: every
    `memory_learn` replaced embedded passages with unembedded ones, and the
    hybrid ranking quietly degraded towards keyword-only for the newest and
    most relevant material.

    Passing ``chunk_ids`` limits the work to one note, which is what the write
    path wants - keeping its own note current is bounded work, while
    backfilling an entire vault is a job for the CLI.
    """
    if not config.semantic:
        return 0
    from .embed import load_backend

    backend = load_backend()
    if backend is None:
        return 0
    rows = list(store.chunks_missing_vectors(chunk_ids))
    if not rows:
        return 0
    done = 0
    batch_size = 32
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        texts = [f"{r['breadcrumb']}\n{r['body']}".strip() for r in batch]
        try:
            vectors = backend.encode(texts)
        except Exception:
            # An embedding failure must never lose the note that was written.
            break
        with store.write():
            for row, vector in zip(batch, vectors):
                store.store_vector(int(row["id"]), vector)
                done += 1
    return done


def index_path(config: Config, store: Store, path: Path, *, force: bool = False) -> IndexReport:
    """Index exactly one note.

    This is what the write tools use. They already know which file changed, so
    rescanning the vault to rediscover it made every ``memory_learn`` cost a
    full directory walk plus a read and a hash of every note in the vault -
    work proportional to the whole collection for a change to one file, paid
    on every single entry. A large vault made recording a lesson slower than
    the lesson was worth.
    """
    report = IndexReport()
    try:
        rel = str(path.relative_to(config.vault))
    except ValueError:
        return report
    if not is_indexable(rel, include=config.include, exclude=config.exclude):
        return report
    report.scanned = 1
    if not path.exists():
        store.delete_note(rel)
        report.removed = 1
        return report
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return report
    digest = digest_of(text)
    previous = store.note_digest(rel)
    if previous == digest and not force:
        report.unchanged = 1
        return report
    _write_note(config, store, path, rel, text, digest)
    if previous is None:
        report.added = 1
    else:
        report.updated = 1
    return report


def reindex(config: Config, store: Store, *, force: bool = False) -> IndexReport:
    """Bring the whole index in line with the vault.

    Two filters, cheapest first. An unchanged mtime skips the file without
    opening it, which is what keeps a startup scan of a large vault fast. When
    the mtime does differ the content digest still decides, so rewriting a file
    with identical contents costs a read but no reindexing - and the mtime is
    recorded anyway so the cheap check works next time.
    """
    report = IndexReport()
    if not config.vault.exists():
        return report

    known = store.note_fingerprints()
    seen: set[str] = set()
    for path in iter_markdown(config):
        rel = str(path.relative_to(config.vault))
        seen.add(rel)
        report.scanned += 1
        previous = known.get(rel)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if previous is not None and not force and previous[0] == mtime:
            report.unchanged += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        digest = digest_of(text)
        if previous is not None and previous[1] == digest and not force:
            store.touch_note(rel, mtime)
            report.unchanged += 1
            continue

        _write_note(config, store, path, rel, text, digest)
        if previous is None:
            report.added += 1
        else:
            report.updated += 1

    for stale in store.all_paths() - seen:
        store.delete_note(stale)
        report.removed += 1

    if store.rebuild_required:
        store.clear_rebuild_flag()
    store.commit()
    return report
