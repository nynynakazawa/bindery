"""The memory tools, and the MCP surface they are exposed through.

The protocol layer is the official SDK's. It was hand-written here to keep the
package dependency-free, and that trade stopped paying: the wire format is the
fastest-moving part of MCP and the least related to what this project is
about. Owning it meant tracking protocol revisions, negotiation, and error
semantics forever, in exchange for one fewer dependency in a package that only
ever runs as a subprocess of an agent that already has far heavier ones.

What is worth owning is below the transport: the tools themselves. Those live
on ``MemoryServer``, which knows nothing about MCP and can be called directly.

The tool surface is deliberately eight tools. Tool schemas are sent to the
model on every session, so each tool has a standing token cost whether or not
it is ever called - a large surface is itself a form of the waste this project
exists to remove.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

# Imported at module scope, not inside build_server: `from __future__ import
# annotations` makes every annotation a string, and the SDK resolves tool
# signatures with eval - which cannot see names bound in a function body.
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from . import __version__
from .config import Config
from .growth import (
    SESSION_PREFIX,
    SessionRecord,
    find_duplicates,
    hot_paths,
    knowledge_gaps,
    promotion_candidates,
    stale_notes,
)
from .indexer import index_path, is_indexable, refresh_embeddings, reindex
from .safeio import update_text
from .search import count_outside_scope, search
from .store import Store
from .tokens import estimate_tokens

#: How many recently-shown paths to remember per session, for turning a later
#: read into a confirmed use. Large enough to span a few searches, small enough
#: that a long session cannot grow without bound.
OFFERED_MEMORY = 200

#: How many past sessions to import per server start. Small on purpose: the
#: cost is paid before the first tool call, and there is always a next startup.
EPISODES_PER_STARTUP = 5



class MemoryServer:
    """Holds the open index for the lifetime of the process."""

    def __init__(self, config: Config) -> None:
        import time

        self.config = config
        self.config.vault.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.db_path)
        # An incremental scan at startup is what keeps two agents consistent
        # when one of them edited notes while the other was not running.
        reindex(self.config, self.store)
        self._import_episodes()
        self.session = SessionRecord(started=time.time(), ended=time.time())
        #: Which agent is on the other end, when it identifies itself during
        #: the handshake. Used only to label session records.
        self.client_name = ""
        self._finalized = False
        self._lock = threading.RLock()
        #: Paths this session has shown in search results. Reading one of them
        #: is what turns an impression into evidence that it was useful.
        #: Bounded because a long session must not accumulate without limit.
        self._offered: dict[str, None] = {}

    def _import_episodes(self) -> None:
        """Capture sessions the other agents finished while we were not running.

        Startup rather than a daemon or an editor hook: whichever agent starts
        next imports whatever the others left behind, so a Claude session that
        ended overnight is searchable from Codex in the morning with nothing
        running in between. Bounded per startup so a first run against years
        of history cannot stall the handshake, and never fatal - failing to
        capture the past must not stop the agent from working now.
        """
        if not self.config.episodes:
            return
        try:
            from .episodes import import_new

            import_new(self.config, self.store, limit=EPISODES_PER_STARTUP)
        except Exception:
            pass

    def _index_written(self, rel: str) -> None:
        """Bring the index up to date for the one note that just changed.

        The other agent must be able to find this immediately - that is the
        whole point of writing it - so this is synchronous, and being scoped to
        a single file is what keeps it affordable to do on every write.
        """
        target = self.config.vault / rel
        index_path(self.config, self.store, target)
        refresh_embeddings(self.config, self.store, self.store.chunk_ids_for(rel))

    # ----------------------------------------------------------- tool impls

    def tool_memory_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "query is required."
        scope = str(args.get("scope", "project")).strip().lower()
        if scope not in {"project", "global", "all"}:
            scope = "project"
        def run(in_scope: str):
            return search(
                self.config,
                self.store,
                query,
                limit=args.get("limit"),
                max_tokens=args.get("max_tokens"),
                scope=in_scope,
            )

        hits, meta = run(scope)
        widened = False
        if not hits and scope == "project" and self.config.project:
            # Narrowing must never be the reason an answer is missed. When the
            # project has nothing to say, the wider memory answers - labelled,
            # so the agent can weigh it as another project's decision rather
            # than as this one's. Without this the first search in a new or
            # renamed project reports an empty memory that is in fact full.
            hits, meta = run("all")
            widened = bool(hits)

        self.session.searches += 1
        if not hits and query not in self.session.unanswered:
            self.session.unanswered.append(query)
        if not hits:
            hint = ""
            if meta["truncated"]:
                hint = f" {meta['truncated']} match(es) were dropped by the token budget; raise max_tokens."
            return f"No passages matched {query!r}.{hint}"

        if widened:
            header = (
                f"{meta['returned']} passage(s), ~{meta['tokens']} tokens. "
                f"Nothing in project '{self.config.project}' matched, so this is "
                "the whole memory - each passage is labelled with the project it "
                "came from, and another project's decision is not this one's."
            )
        else:
            header = f"{meta['returned']} passage(s), ~{meta['tokens']} tokens{self._scope_label(scope)}."
        lines = [header]
        if meta["truncated"]:
            lines.append(f"({meta['truncated']} further match(es) omitted to stay inside the budget.)")
        lines.append("")
        for hit in hits:
            location = f"{hit.chunk.path}" + (f" # {hit.chunk.breadcrumb}" if hit.chunk.breadcrumb else "")
            origin = f"  [{hit.chunk.project}]" if hit.chunk.project else "  [global]"
            lines.append(f"--- {location}{origin}  [{hit.matched_by}]")
            lines.append(hit.chunk.body)
            lines.append("")
        self._remember_offered(hit.chunk.path for hit in hits)
        tail = "" if widened else self._widen_hint(query, scope)
        return "\n".join(lines).rstrip() + tail

    def _remember_offered(self, paths) -> None:
        for path in paths:
            self._offered.pop(path, None)
            self._offered[path] = None
        while len(self._offered) > OFFERED_MEMORY:
            self._offered.pop(next(iter(self._offered)))

    def _project_source(self) -> str:
        from .workspace import resolve

        try:
            return resolve(state_dir=self.config.state_dir).describe()
        except Exception:  # pragma: no cover - status must never fail on this
            return "unknown"

    def _scope_label(self, scope: str) -> str:
        if scope == "all" or not self.config.project:
            return ""
        if scope == "global":
            return " (cross-project notes only)"
        return f" (project '{self.config.project}' + cross-project notes)"

    def _widen_hint(self, query: str, scope: str) -> str:
        """Tell the agent when the answer exists but sits outside the scope.

        A narrow default is only safe if it is visible. Otherwise a scoped
        search that finds nothing looks exactly like an empty memory, and the
        agent stops looking - which would make scoping worse than not having
        it.
        """
        if scope != "project" or not self.config.project:
            return ""
        elsewhere = count_outside_scope(self.config, self.store, query)
        if not elsewhere:
            return ""
        return (
            f"\n\n({elsewhere} more passage(s) exist in other projects. "
            'Repeat with scope="all" if this question is not project-specific.)'
        )

    def tool_memory_read(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        target = self._safe_path(rel)
        if target is None:
            return f"Refused: {rel!r} is outside the vault."
        if not is_indexable(rel, include=self.config.include, exclude=self.config.exclude):
            # Otherwise the allowlist would only cover search, and any path an
            # agent guessed - or read in a wiki link - would walk straight
            # through it.
            return f"Refused: {rel!r} is outside the configured index boundary."
        if not target.exists():
            return f"Not found: {rel}"
        if self._offered.pop(rel, "missing") is None:
            # Followed a search result through to the note. That is the one
            # thing this server can observe that distinguishes "we showed it"
            # from "it helped", so it is the only signal that trains ranking.
            self.store.record_use([rel])
            self.store.commit()
        text = target.read_text(encoding="utf-8")
        cap = args.get("max_tokens") or self.config.max_tokens
        if estimate_tokens(text) <= cap:
            return text
        # Truncate by line so the result stays valid Markdown.
        kept: list[str] = []
        spent = 0
        for line in text.splitlines():
            cost = estimate_tokens(line)
            if spent + cost > cap:
                break
            kept.append(line)
            spent += cost
        kept.append(f"\n[truncated at ~{cap} tokens - raise max_tokens for the rest]")
        return "\n".join(kept)

    def tool_memory_write(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        if not rel.endswith(".md"):
            rel = f"{rel}.md"
        target = self._safe_path(rel)
        if target is None:
            return f"Refused: {rel!r} is outside the vault."
        if not is_indexable(rel, include=self.config.include, exclude=self.config.exclude):
            return f"Refused: {rel!r} is outside the configured index boundary."
        content = str(args.get("content", ""))
        title = str(args.get("title", "")).strip()
        tags = args.get("tags") or []
        # Recorded in the note itself rather than inferred from where it sits,
        # so that moving the file does not change what it is understood to be
        # about. Passing an empty string marks knowledge that spans projects.
        project = args.get("project")
        project = self.config.project if project is None else str(project).strip()

        # `project` is written whenever the caller said anything about it,
        # including an empty string - that is how a note is marked as applying
        # everywhere rather than to the folder it happens to sit in.
        declare_project = args.get("project") is not None or bool(project)
        front: list[str] = []
        if title or tags or declare_project:
            front.append("---")
            if title:
                front.append(f"title: {title}")
            if declare_project:
                front.append(f"project: {project}")
            if tags:
                front.append("tags: [" + ", ".join(str(t) for t in tags) + "]")
            front.append("---")
            front.append("")
        target.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join([*front, content]).rstrip() + "\n"
        # Locked even though this is a whole-file replace, so that it cannot
        # land in the middle of a concurrent read-modify-write of the same note.
        update_text(target, lambda _current: body, lock_dir=self.config.state_dir)

        self._index_written(rel)
        if args.get("pin"):
            self.store.set_pinned(rel, True)
            self.store.commit()
        if rel not in self.session.written:
            self.session.written.append(rel)
        return f"Wrote {rel} (~{estimate_tokens(content)} tokens) and indexed it."

    def tool_memory_learn(self, args: dict[str, Any]) -> str:
        """Append to today's journal - the episodic layer of the memory.

        Entries land in one file per day rather than one file per entry, which
        keeps the vault navigable in Obsidian and makes a tag's note count mean
        "turned up on N separate days" rather than "was mentioned N times".
        """
        import datetime

        content = str(args.get("content", "")).strip()
        if not content:
            return "content is required."
        raw_tags = args.get("tags") or []
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]

        today = datetime.date.today().isoformat()
        project = args.get("project")
        project = self.config.project if project is None else str(project).strip()
        # One journal per project per day. A single global journal would make
        # every day's entries a mixture of unrelated codebases, which is the
        # one file a scoped search can never usefully filter.
        rel = f"journal/{project}/{today}.md" if project else f"journal/{today}.md"
        target = self._safe_path(rel)
        if target is None:
            return "Refused: journal path resolved outside the vault."
        target.parent.mkdir(parents=True, exist_ok=True)

        stamp = datetime.datetime.now().strftime("%H:%M")

        def append_entry(current: str) -> str:
            """Merge one entry into whatever is on disk *right now*.

            Runs under the journal's lock, so `current` is the version written
            by any agent that got here first - which is exactly what makes a
            concurrent entry an addition rather than an overwrite.
            """
            from .indexer import parse_frontmatter

            existing_tags: list[str] = []
            entries = ""
            if current:
                meta, entries = parse_frontmatter(current)
                existing_tags = [
                    t.strip()
                    for t in meta.get("tags", "").strip("[]").split(",")
                    if t.strip()
                ]

            merged = sorted({*existing_tags, *tags})
            header = ["---", f"title: Journal {today}"]
            if project:
                header.append(f"project: {project}")
            if merged:
                header.append("tags: [" + ", ".join(merged) + "]")
            header += ["---", ""]

            body = entries.rstrip() or f"# Journal {today}"
            body += f"\n\n## {stamp}\n\n{content}"
            entry_tags = " ".join(f"#{t}" for t in tags)
            if entry_tags:
                body += f"\n\n{entry_tags}"
            # header already ends in a blank line; body starts at the H1.
            return "\n".join(header) + body.lstrip("\n") + "\n"

        update_text(target, append_entry, lock_dir=self.config.state_dir)
        self._index_written(rel)
        self.session.learned += 1
        return f"Recorded in {rel} (~{estimate_tokens(content)} tokens)." + (
            f" Tags: {', '.join(tags)}." if tags else ""
        )

    def tool_memory_review(self, args: dict[str, Any]) -> str:
        gaps = knowledge_gaps(self.store)
        hot = hot_paths(self.store)
        duplicates = find_duplicates(self.store)
        stale = stale_notes(self.store)
        promote = promotion_candidates(self.store)
        unread = self.store.shown_but_unread()

        report: dict[str, Any] = {
            "knowledge_gaps": [
                {"query": g.query, "asked": g.count} for g in gaps
            ],
            "load_bearing_notes": [
                {"path": path, "times_read": uses, "weight": round(score, 3)}
                for path, uses, score in hot
            ],
            # Shown often, never opened. Either the passage answers the
            # question on its own, or the note keeps matching queries it has
            # nothing to say about - worth a human eye either way.
            "shown_but_never_read": [
                {"path": path, "times_shown": shown} for path, shown in unread
            ],
            "near_duplicate_total": len(duplicates),
            "near_duplicates": [
                {
                    "a": f"{d.left}" + (f" # {d.left_heading}" if d.left_heading else ""),
                    "b": f"{d.right}" + (f" # {d.right_heading}" if d.right_heading else ""),
                    "similarity": d.similarity,
                }
                for d in duplicates[:10]
            ],
            "stale_notes": [{"path": path, "age_days": age} for path, age in stale],
            "promotion_candidates": [
                {"tag": c.tag, "journal_entries": c.entries} for c in promote
            ],
        }
        advice: list[str] = []
        if gaps:
            advice.append(
                f"{len(gaps)} recurring question(s) had no answer - write notes covering them."
            )
        if duplicates:
            advice.append(
                f"{len(duplicates)} near-duplicate pair(s) - merging them cuts retrieval noise."
            )
        if promote:
            advice.append(
                f"{len(promote)} journal topic(s) recur often enough to deserve their own note."
            )
        if unread:
            advice.append(
                f"{len(unread)} note(s) keep appearing in results but are never read - "
                "check whether they match queries they cannot answer."
            )
        if not advice:
            advice.append("Nothing needs attention.")
        report["next_actions"] = advice
        return json.dumps(report, ensure_ascii=False, indent=2)

    def tool_memory_links(self, args: dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        row = self.store.conn.execute(
            "SELECT id, title FROM notes WHERE path=?", (rel,)
        ).fetchone()
        if row is None:
            return f"Not indexed: {rel}"
        outgoing = [
            r["target"]
            for r in self.store.conn.execute(
                "SELECT DISTINCT target FROM links WHERE src_note_id=? ORDER BY target", (row["id"],)
            )
        ]
        # A back link is any note whose [[target]] resolves to this note's
        # title or to its filename, which is how Obsidian resolves them too.
        stem = Path(rel).stem
        incoming = [
            r["path"]
            for r in self.store.conn.execute(
                "SELECT DISTINCT n.path FROM links l JOIN notes n ON n.id = l.src_note_id "
                "WHERE l.target IN (?, ?) AND n.path <> ? ORDER BY n.path",
                (row["title"], stem, rel),
            )
        ]
        return json.dumps(
            {"path": rel, "title": row["title"], "links_to": outgoing, "linked_from": incoming},
            ensure_ascii=False,
            indent=2,
        )

    def tool_memory_status(self, args: dict[str, Any]) -> str:
        from .embed import load_backend

        backend = load_backend() if self.config.semantic else None
        stats = self.store.stats()
        return json.dumps(
            {
                "version": __version__,
                "vault": str(self.config.vault),
                "index": str(self.config.db_path),
                "notes": stats["notes"],
                "passages": stats["chunks"],
                "links": stats["links"],
                "default_max_tokens": self.config.max_tokens,
                "semantic_search": backend.name if backend else "off (keyword only)",
                "embedded_passages": stats["vectors"],
                "vector_index": "sqlite-vec" if self.store.ann_enabled else "exact scan",
                "project": self.config.project or "(none - every search is global)",
                # How that name was decided. Without it, "why is this project
                # called Sales" needs a source dive.
                "project_source": self._project_source(),
                "projects_indexed": dict(self.store.projects()),
                "indexed_only": self.config.include or "(whole vault)",
                "never_indexed": self.config.exclude or [],
            },
            ensure_ascii=False,
            indent=2,
        )

    def tool_memory_reindex(self, args: dict[str, Any]) -> str:
        report = reindex(self.config, self.store, force=bool(args.get("force")))
        return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)

    # -------------------------------------------------------------- helpers

    def _safe_path(self, rel: str) -> Path | None:
        """Resolve ``rel`` inside the vault, refusing traversal outside it."""
        if not rel:
            return None
        candidate = (self.config.vault / rel).resolve()
        try:
            candidate.relative_to(self.config.vault)
        except ValueError:
            return None
        return candidate

    def finalize_session(self) -> str | None:
        """Write the automatic session record, if the session earned one.

        This runs when the client disconnects. It records only what the server
        directly observed - no summarising, no model call - so it is honest
        about being an activity log rather than a set of insights. Sessions
        that produced fewer than ``AUTO_CAPTURE_MIN_SIGNALS`` signals write
        nothing at all, which is what stops routine lookups from filling the
        vault with noise.
        """
        with self._lock:
            return self._finalize_locked()

    def _finalize_locked(self) -> str | None:
        import datetime
        import time

        if self._finalized or not self.config.autocapture:
            return None
        self._finalized = True
        self.session.ended = time.time()
        if not self.session.worth_recording():
            return None

        rel = f"{SESSION_PREFIX}{datetime.date.today().isoformat()}.md"
        target = self._safe_path(rel)
        if target is None:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)

        today = datetime.date.today().isoformat()
        rendered = self.session.render(client=self.client_name)

        def append_record(current: str) -> str:
            from .indexer import parse_frontmatter

            existing = ""
            if current:
                _, existing = parse_frontmatter(current)
            header = "---\ntitle: Sessions {0}\n---\n\n".format(today)
            body = existing.rstrip() or f"# Sessions {today}"
            body += "\n\n" + rendered
            return header + body.lstrip("\n") + "\n"

        update_text(target, append_record, lock_dir=self.config.state_dir)

        try:
            index_path(self.config, self.store, target)
            self.store.prune_queries()
            self.store.commit()
        except Exception:
            # A failure to index the record must never take down a shutdown.
            pass
        return rel

    def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """The one entry point to the tools, and the one place they serialise.

        The SDK runs synchronous tools on a worker thread, so two calls can
        overlap in a way the old single-threaded stdio loop never allowed.
        SQLite connections, the index, and the session record are all shared
        mutable state, so calls take their turn rather than each component
        growing its own locking.
        """
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            raise KeyError(name)
        with self._lock:
            return handler(args)


# --------------------------------------------------------------- MCP surface

#: Written out rather than taken from docstrings: these strings are sent to the
#: model on every session, so they are part of the token budget this project
#: exists to defend, and they get edited as copy rather than as documentation.
_SEARCH_DOC = (
    "Search the shared memory and return only the passages that match, "
    "under a hard token budget. Use this instead of reading whole notes. "
    "Works in Japanese and English."
)
_READ_DOC = (
    "Read one note in full by its vault-relative path. Use after memory_search "
    "when a passage is not enough."
)
_WRITE_DOC = (
    "Create or overwrite a note and index it immediately, so the other agent "
    "can find it straight away. Content is plain Markdown."
)
_LEARN_DOC = (
    "Record something learned during this session - a decision, a constraint, a "
    "dead end, a fix that worked. Appends to today's journal and indexes it "
    "immediately. Call this whenever you learn something the next session would "
    "otherwise have to rediscover. Tags are what let recurring topics graduate "
    "into durable notes."
)
_REVIEW_DOC = (
    "Report how the memory is growing: what agents searched for and could not "
    "find, which notes carry the load, near-duplicate notes, stale notes, and "
    "recurring journal topics that should become durable notes."
)
_LINKS_DOC = (
    "List the notes a note links to and the notes that link back, following "
    "[[wiki links]]."
)
_STATUS_DOC = (
    "Report vault location, index size, the current project, the index "
    "boundary, and whether semantic search is active."
)
_REINDEX_DOC = "Rescan the vault. Only needed after editing notes outside of an agent."

_SCOPE_DOC = (
    "Which memory to search. 'project' (default) is this codebase plus notes "
    "that apply everywhere; 'global' is only the cross-project notes; 'all' "
    "ignores project boundaries. A decision from another repository is not "
    "evidence about this one, so widen deliberately."
)
_PROJECT_DOC = (
    "Which codebase this belongs to. Defaults to the current one. Pass an "
    "empty string for knowledge that is true everywhere."
)


def build_server(config: Config):
    """Wire the memory tools onto an MCP server.

    The server owns one ``MemoryServer`` for the life of the process, which is
    what keeps the index open rather than reopening it per call.
    """
    memory = MemoryServer(config)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            # EOF on stdin is the only moment the server reliably knows the
            # session is over, and it is where automatic capture happens.
            memory.finalize_session()

    mcp = MCPServer("bindery", version=__version__, lifespan=lifespan)

    def _note_client(ctx: Context) -> None:
        """Label session records with whichever agent is on the other end.

        Both spellings are accepted because the SDK has carried this field
        under each at different times, and a session record losing its label is
        not worth an exception - the record itself still gets written.
        """
        try:
            params = ctx.session.client_params
            info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
            name = str(getattr(info, "name", "") or "")
            if name:
                memory.client_name = name
        except Exception:
            pass

    # structured_output=False on every tool. These return prose for the model
    # to read, and a `str` return type otherwise makes the SDK also emit a
    # structuredContent block containing the identical string - doubling the
    # size of every response in a project whose entire purpose is to keep
    # responses small.
    @mcp.tool(name="memory_search", description=_SEARCH_DOC, structured_output=False)
    def memory_search(
        ctx: Context,
        query: Annotated[str, Field(description="What to look for.")],
        scope: Annotated[Literal["project", "global", "all"], Field(description=_SCOPE_DOC)] = "project",
        limit: Annotated[int | None, Field(description="Maximum passages to return.")] = None,
        max_tokens: Annotated[int | None, Field(description="Hard cap on the size of the response.")] = None,
    ) -> str:
        _note_client(ctx)
        return memory.call_tool("memory_search",
            {"query": query, "scope": scope, "limit": limit, "max_tokens": max_tokens}
        )

    @mcp.tool(name="memory_read", description=_READ_DOC, structured_output=False)
    def memory_read(
        path: Annotated[str, Field(description="Vault-relative path, e.g. 'project/decision.md'.")],
        max_tokens: Annotated[int | None, Field(description="Truncate the note at this size.")] = None,
    ) -> str:
        return memory.call_tool("memory_read", {"path": path, "max_tokens": max_tokens})

    @mcp.tool(name="memory_write", description=_WRITE_DOC, structured_output=False)
    def memory_write(
        path: Annotated[str, Field(description="Vault-relative path ending in .md")],
        content: Annotated[str, Field(description="Markdown body.")],
        title: Annotated[str, Field(description="Optional front matter title.")] = "",
        tags: Annotated[list[str] | None, Field(description="Optional front matter tags.")] = None,
        pin: Annotated[
            bool,
            Field(description="Mark as durable so it always ranks high and never goes stale."),
        ] = False,
        project: Annotated[str | None, Field(description=_PROJECT_DOC)] = None,
    ) -> str:
        return memory.call_tool("memory_write",
            {
                "path": path, "content": content, "title": title,
                "tags": tags or [], "pin": pin, "project": project,
            }
        )

    @mcp.tool(name="memory_learn", description=_LEARN_DOC, structured_output=False)
    def memory_learn(
        ctx: Context,
        content: Annotated[str, Field(description="What was learned, in Markdown.")],
        tags: Annotated[
            list[str] | None,
            Field(description="Topic tags, e.g. ['auth', 'deployment']."),
        ] = None,
        project: Annotated[str | None, Field(description=_PROJECT_DOC)] = None,
    ) -> str:
        _note_client(ctx)
        return memory.call_tool("memory_learn",
            {"content": content, "tags": tags or [], "project": project}
        )

    @mcp.tool(name="memory_review", description=_REVIEW_DOC, structured_output=False)
    def memory_review() -> str:
        return memory.call_tool("memory_review", {})

    @mcp.tool(name="memory_links", description=_LINKS_DOC, structured_output=False)
    def memory_links(path: Annotated[str, Field(description="Vault-relative path.")]) -> str:
        return memory.call_tool("memory_links", {"path": path})

    @mcp.tool(name="memory_status", description=_STATUS_DOC, structured_output=False)
    def memory_status() -> str:
        return memory.call_tool("memory_status", {})

    @mcp.tool(name="memory_reindex", description=_REINDEX_DOC, structured_output=False)
    def memory_reindex(
        force: Annotated[bool, Field(description="Rebuild every note.")] = False,
    ) -> str:
        return memory.call_tool("memory_reindex", {"force": force})

    return mcp, memory


def serve(config: Config) -> None:
    """Run the MCP server on stdio until the client disconnects."""
    mcp, _memory = build_server(config)
    mcp.run("stdio")
