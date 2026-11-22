"""Read-only JSON payloads for the Bindery.app dashboard."""

from __future__ import annotations

import datetime
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from .config import Config
from .indexer import FENCE_RE, HEADING_RE, note_title, parse_frontmatter, parse_tags
from .redact import redact
from .search import search_with_project_fallback
from .store import SCHEMA_VERSION, Store

MAX_NOTE_BYTES = 2 * 1024 * 1024
DASHBOARD_LIMIT = 20
_JOURNAL_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\.[^.]+)?")
_ENTRY_TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

EMPTY_PAYLOADS = {
    "search": {"results": []},
    "notes": {
        "notes": [],
        "totals": {"notes": 0, "chunks": 0, "vectors": 0, "links": 0, "projects": {}},
    },
    "read": {"path": "", "title": "", "frontmatter": {}, "body": "", "mtime": 0.0},
    "recent": {"entries": []},
    "ranking": {"most_used": [], "most_shown": [], "top_queries": []},
}


class DashboardError(ValueError):
    """An error safe to return to the dashboard."""


def payload(
    config: Config,
    command: str,
    *,
    value: str = "",
    limit: int | None = None,
    max_tokens: int | None = None,
) -> dict:
    if limit is not None and limit < 0:
        raise DashboardError("--limit は 0 以上を指定してください")
    if max_tokens is not None and max_tokens <= 0:
        raise DashboardError("--max-tokens は 1 以上を指定してください")
    count = DASHBOARD_LIMIT if limit is None else limit
    if command == "read":
        return _read(config.vault, value)
    if command == "recent":
        return {"entries": _recent(config.vault, count)}
    if not config.db_path.is_file():
        raise DashboardError("未索引")
    with closing(Store(config.db_path, read_only=True)) as store:
        # All rows and counts in a response describe the same index revision,
        # even while an agent is editing notes or syncing in the background.
        store.conn.execute("BEGIN")
        if store.get_meta("schema_version") != str(SCHEMA_VERSION) or store.rebuild_required:
            raise DashboardError("索引の更新が必要です。bindery index を実行してください")
        if command == "notes":
            return _notes(store)
        if command == "ranking":
            return _ranking(store, count)
        return _search(config, store, value, limit, max_tokens)


def _search(config: Config, store: Store, query: str, limit: int | None, max_tokens: int | None) -> dict:
    query = query.strip()
    if not query:
        raise DashboardError("検索語を指定してください")
    if limit == 0:
        return {"results": []}
    hits, _, _ = search_with_project_fallback(
        config, store, query, limit=limit, max_tokens=max_tokens, learn=False,
    )
    return {"results": [
        {
            "path": hit.chunk.path,
            "heading": redact(hit.chunk.breadcrumb),
            "snippet": redact(hit.chunk.body),
            "match": hit.matched_by,
            "score": hit.score,
        }
        for hit in hits
    ]}


def _notes(store: Store) -> dict:
    rows = store.conn.execute(
        """SELECT n.path, n.title, n.project, n.tags, n.tier, n.mtime,
                  (SELECT COUNT(*) FROM chunks WHERE note_id = n.id) AS chunks,
                  COALESCE(u.uses, 0) AS uses,
                  COALESCE(u.impressions, 0) AS impressions,
                  COALESCE(u.last_used, 0.0) AS last_used
           FROM notes n LEFT JOIN usage u ON u.path = n.path
           ORDER BY n.path"""
    )
    notes = []
    for row in rows:
        note = dict(row)
        note["title"] = redact(note["title"])
        note["project"] = redact(note["project"])
        tags = json.loads(note["tags"])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise DashboardError("索引のタグを読み取れません")
        note["tags"] = [redact(tag) for tag in tags]
        notes.append(note)
    projects: dict[str, int] = {}
    for name, count in store.projects():
        label = redact(name)
        projects[label] = projects.get(label, 0) + count
    return {"notes": notes, "totals": {**store.stats(), "projects": projects}}


def _ranking(store: Store, limit: int) -> dict:
    def by_usage(metric: str) -> list[dict]:
        rows = store.conn.execute(
            f"""SELECT n.path, n.title, u.{metric}
                FROM usage u JOIN notes n ON n.path = u.path
                WHERE u.{metric} > 0
                ORDER BY u.{metric} DESC, n.path LIMIT ?""",
            (limit,),
        )
        return [{**row, "title": redact(row["title"])} for row in rows]

    queries = store.conn.execute(
        """SELECT text, COUNT(*) AS count, AVG(hits) AS avg_hits
           FROM queries GROUP BY text ORDER BY count DESC, text LIMIT ?""",
        (limit,),
    )
    return {
        "most_used": by_usage("uses"),
        "most_shown": by_usage("impressions"),
        "top_queries": [{**row, "text": redact(row["text"])} for row in queries],
    }


def _open_resolved(vault: Path, relative: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if os.open not in os.supports_dir_fd:
        return os.open(vault / relative, flags)

    # Resolve first to allow links within the vault. Walk the resolved path
    # from the vault descriptor without following any newly swapped-in links.
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(vault, directory_flags)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(relative.name, flags | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def _read_markdown(vault: Path, relative: Path) -> tuple[str, float]:
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".md":
        raise DashboardError("Vault 内の .md ファイルを相対パスで指定してください")
    vault = vault.resolve(strict=True)
    try:
        resolved = (vault / relative).resolve(strict=True).relative_to(vault)
    except ValueError:
        raise DashboardError("ファイルの実体が Vault の外にあります") from None
    if resolved.suffix.lower() != ".md":
        raise DashboardError("ファイルの実体が .md ではありません")
    descriptor = _open_resolved(vault, resolved)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise DashboardError("通常の .md ファイルを指定してください")
        if info.st_size > MAX_NOTE_BYTES:
            raise DashboardError("ファイルが 2 MB を超えています")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_NOTE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_NOTE_BYTES:
        raise DashboardError("ファイルが 2 MB を超えています")
    try:
        return data.decode("utf-8"), info.st_mtime
    except UnicodeDecodeError:
        raise DashboardError("ファイルが UTF-8 ではありません") from None


def _read(vault: Path, value: str) -> dict:
    relative = Path(value)
    text, mtime = _read_markdown(vault, relative)
    meta, body = parse_frontmatter(redact(text))
    frontmatter: dict[str, str | list[str]] = dict(meta)
    if "tags" in meta:
        frontmatter["tags"] = [redact(tag) for tag in parse_tags(meta["tags"])]
    return {
        "path": relative.as_posix(),
        "title": redact(note_title(relative, meta, body)),
        "frontmatter": frontmatter,
        "body": body,
        "mtime": mtime,
    }


def _recent(vault: Path, limit: int) -> list[dict]:
    journal = vault / "journal"
    if limit == 0 or journal.is_symlink() or not journal.is_dir():
        return []
    journal_days: dict[str, list[Path]] = {}
    for root, dirs, files in os.walk(journal):
        if Path(root) == journal and "sessions" in dirs:
            dirs.remove("sessions")
        for filename in files:
            path = Path(root, filename)
            match = _JOURNAL_DATE.fullmatch(path.stem) if path.suffix.lower() == ".md" else None
            if match is None or path.is_symlink():
                continue
            try:
                date = datetime.date.fromisoformat(match[1]).isoformat()
            except ValueError:
                continue
            journal_days.setdefault(date, []).append(path.relative_to(vault))

    entries = []
    for date in sorted(journal_days, reverse=True):
        day_entries = []
        for path in journal_days[date]:
            try:
                text, mtime = _read_markdown(vault, path)
            except (OSError, ValueError, RuntimeError):
                continue
            meta, body = parse_frontmatter(redact(text))
            tags = [redact(tag) for tag in parse_tags(meta.get("tags", ""))]
            rel = path.as_posix()
            for line_number, heading in _journal_headings(body):
                time = heading if _ENTRY_TIME.fullmatch(heading) else ""
                entry = {"path": rel, "heading": heading, "date": date, "tags": tags}
                day_entries.append(((time, mtime, rel, line_number), entry))
        day_entries.sort(key=lambda item: item[0], reverse=True)
        entries.extend(entry for _, entry in day_entries[:limit - len(entries)])
        if len(entries) == limit:
            break
    return entries


def _journal_headings(body: str) -> Iterator[tuple[int, str]]:
    fence = ""
    for line_number, line in enumerate(body.splitlines()):
        marker = FENCE_RE.match(line)
        if marker:
            character = marker[1][0]
            fence = "" if fence == character else fence or character
            continue
        heading = None if fence else HEADING_RE.match(line)
        if heading and heading[1] == "##" and heading[2].strip():
            yield line_number, heading[2].strip()
