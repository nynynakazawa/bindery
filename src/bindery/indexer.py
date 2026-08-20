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


@dataclass(slots=True)
class ParsedNote:
    title: str
    tags: list[str]
    body: str
    links: list[str] = field(default_factory=list)


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
    return ParsedNote(title=title, tags=tags, body=body, links=links)


def chunk_markdown(body: str, *, max_tokens: int, overlap: int) -> list[tuple[str, str, int]]:
    """Split ``body`` into ``(heading, text, tokens)`` passages.

    Sections are cut at Markdown headings first, because a heading is the
    author's own statement about where one idea ends and the next begins.
    Oversized sections are then split by line with a small overlap so that a
    fact sitting on a boundary is still retrievable.
    """
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current: list[str] = []
    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match:
            if current:
                sections.append((current_heading, current))
            current_heading = match.group(2).strip()
            current = []
        else:
            current.append(line)
    if current:
        sections.append((current_heading, current))

    chunks: list[tuple[str, str, int]] = []
    for heading, lines in sections:
        buffer: list[str] = []
        used = 0
        for line in lines:
            cost = estimate_tokens(line)
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
                buffer = [*tail, line]
                used = kept + cost
            else:
                buffer.append(line)
                used += cost
        text = "\n".join(buffer).strip()
        if text:
            chunks.append((heading, text, used))
    return chunks


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def is_indexable(rel: str) -> bool:
    return not rel.replace("\\", "/").startswith(SKIP_PREFIXES)


def iter_markdown(vault: Path):
    for path in sorted(vault.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not is_indexable(str(path.relative_to(vault))):
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


def reindex(config: Config, store: Store, *, force: bool = False) -> IndexReport:
    """Bring the index in line with the vault.

    Content digests rather than mtimes decide what changed, so a file that is
    touched but not edited costs nothing to re-scan.
    """
    report = IndexReport()
    if not config.vault.exists():
        return report

    seen: set[str] = set()
    for path in iter_markdown(config.vault):
        rel = str(path.relative_to(config.vault))
        seen.add(rel)
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        digest = digest_of(text)
        previous = store.note_digest(rel)
        if previous == digest and not force:
            report.unchanged += 1
            continue

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
            mtime=path.stat().st_mtime,
            digest=digest,
            chunks=chunks,
            links=note.links,
        )
        if previous is None:
            report.added += 1
        else:
            report.updated += 1

    for stale in store.all_paths() - seen:
        store.delete_note(stale)
        report.removed += 1

    store.commit()
    return report
