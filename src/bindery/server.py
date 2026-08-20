"""MCP server over stdio.

The Model Context Protocol stdio transport is newline-delimited JSON-RPC 2.0.
Implementing it directly keeps this package dependency-free, which is the point:
the server has to start reliably inside both Claude Code and Codex, and every
dependency is one more thing that can be missing in one of those environments
but not the other.

The tool surface is deliberately six tools. Tool schemas are sent to the model
on every session, so each tool has a standing token cost whether or not it is
ever called - a large surface is itself a form of the waste this project exists
to remove.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

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

PROTOCOL_VERSION = "2025-06-18"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "Search the shared memory and return only the passages that match, "
            "under a hard token budget. Use this instead of reading whole notes. "
            "Works in Japanese and English."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "scope": {
                    "type": "string",
                    "enum": ["project", "global", "all"],
                    "description": (
                        "Which memory to search. 'project' (default) is this codebase plus "
                        "notes that apply everywhere; 'global' is only the cross-project "
                        "notes; 'all' ignores project boundaries. A decision from another "
                        "repository is not evidence about this one, so widen deliberately."
                    ),
                },
                "limit": {"type": "integer", "description": "Maximum passages to return."},
                "max_tokens": {
                    "type": "integer",
                    "description": "Hard cap on the size of the response.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_read",
        "description": "Read one note in full by its vault-relative path. Use after memory_search when a passage is not enough.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path, e.g. 'project/decision.md'."},
                "max_tokens": {"type": "integer", "description": "Truncate the note at this size."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Create or overwrite a note and index it immediately, so the other agent "
            "can find it straight away. Content is plain Markdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path ending in .md"},
                "content": {"type": "string", "description": "Markdown body."},
                "title": {"type": "string", "description": "Optional front matter title."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional front matter tags.",
                },
                "pin": {
                    "type": "boolean",
                    "description": "Mark as durable so it always ranks high and never goes stale.",
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Which codebase this note is about. Defaults to the current one. "
                        "Pass an empty string for knowledge that is true everywhere."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "memory_learn",
        "description": (
            "Record something learned during this session - a decision, a constraint, a "
            "dead end, a fix that worked. Appends to today's journal and indexes it "
            "immediately. Call this whenever you learn something the next session would "
            "otherwise have to rediscover. Tags are what let recurring topics graduate "
            "into durable notes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What was learned, in Markdown."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topic tags, e.g. ['auth', 'deployment'].",
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Which codebase this applies to. Defaults to the current one. "
                        "Pass an empty string for a lesson that is not project-specific."
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_review",
        "description": (
            "Report how the memory is growing: what agents searched for and could not "
            "find, which notes carry the load, near-duplicate notes, stale notes, and "
            "recurring journal topics that should become durable notes."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_links",
        "description": "List the notes a note links to and the notes that link back, following [[wiki links]].",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "memory_status",
        "description": "Report vault location, index size, the current project, the index boundary, and whether semantic search is active.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_reindex",
        "description": "Rescan the vault. Only needed after editing notes outside of an agent.",
        "inputSchema": {
            "type": "object",
            "properties": {"force": {"type": "boolean", "description": "Rebuild every note."}},
        },
    },
]


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
        self.session = SessionRecord(started=time.time(), ended=time.time())
        #: Which agent is on the other end, when it identifies itself during
        #: the handshake. Used only to label session records.
        self.client_name = ""
        self._finalized = False

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
            location = f"{hit.chunk.path}" + (f" # {hit.chunk.heading}" if hit.chunk.heading else "")
            origin = f"  [{hit.chunk.project}]" if hit.chunk.project else "  [global]"
            lines.append(f"--- {location}{origin}  [{hit.matched_by}]")
            lines.append(hit.chunk.body)
            lines.append("")
        tail = "" if widened else self._widen_hint(query, scope)
        return "\n".join(lines).rstrip() + tail

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

        front: list[str] = []
        if title or tags or project:
            front.append("---")
            if title:
                front.append(f"title: {title}")
            if project:
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

        report: dict[str, Any] = {
            "knowledge_gaps": [
                {"query": g.query, "asked": g.count} for g in gaps
            ],
            "load_bearing_notes": [
                {"path": path, "retrievals": uses, "weight": round(score, 3)}
                for path, uses, score in hot
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
                "project": self.config.project or "(none - every search is global)",
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
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            raise KeyError(name)
        return handler(args)

    # ------------------------------------------------------------ dispatch

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")

        if method == "initialize":
            info = (message.get("params") or {}).get("clientInfo") or {}
            self.client_name = str(info.get("name", "")).strip()
            return _result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bindery", "version": __version__},
            })
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                text = self.call_tool(name, args)
            except KeyError:
                return _error(msg_id, -32601, f"Unknown tool: {name}")
            except Exception as exc:  # surfaced to the model, not swallowed
                return _result(msg_id, {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                })
            return _result(msg_id, {"content": [{"type": "text", "text": text}]})

        if msg_id is None:
            return None
        return _error(msg_id, -32601, f"Unknown method: {method}")


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(config: Config, stdin=None, stdout=None) -> None:
    """Run the stdio loop until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = MemoryServer(config)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(message)
        if response is None:
            continue
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()

    # EOF: the client went away. This is the only moment the server reliably
    # knows a session is over, so it is where automatic capture happens.
    server.finalize_session()
