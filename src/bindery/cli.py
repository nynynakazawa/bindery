"""Command line interface.

`serve` is what the agents run. The remaining commands exist so that a human
can inspect and repair the index without going through an agent, which matters
when the thing being debugged is the agent's own memory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import subprocess
import datetime as _dt
import tomllib
import shlex
from pathlib import Path

from . import __version__
from .config import Config, _clean_prefixes
from .episodes import set_baseline
from .indexer import index_path, reindex
from .search import search
from .safeio import ConcurrentUpdate, atomic_config_update, atomic_write, update_text
from .store import SCHEMA_VERSION, Store
from .workspace import MARKER as MARKER_HINT
from .workspace import resolve as resolve_project


class ConfigWriteError(RuntimeError):
    """A requested configuration update could not be applied safely."""


def _add_common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    absent = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--vault", default=absent,
                        help="Directory of Markdown notes (default: $BINDERY_VAULT).")
    parser.add_argument("--state-dir", default=absent, help="Where to keep the index.")
    parser.add_argument("--no-semantic", action="store_true", default=absent,
                        help="Keyword search only.")
    parser.add_argument(
        "--include",
        action="append",
        default=absent,
        metavar="DIR",
        help=(
            "Index ONLY this vault-relative directory. Repeatable. Naming any "
            "directory excludes everything else - use it when the vault holds "
            "more than work notes."
        ),
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=absent,
        metavar="DIR",
        help="Never index this vault-relative directory. Repeatable.",
    )



def _add_project(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Name the codebase whose memory this command is about.

    Every subcommand takes it, and it means the same thing in all of them. It
    briefly did not: `install` and `setup` used `--project` for which config
    file to write, so `setup --project alpha` was a parse error rather than a
    way to name a project. That flag is `--local` now.
    """
    parser.add_argument(
        "--project",
        default=argparse.SUPPRESS if suppress_defaults else None,
        help=(
            "Name the codebase these notes belong to (default: detected from the "
            "git remote of the working directory). Pass '' to disable scoping."
        ),
    )


def _vault_from_registered_clients() -> str | None:
    """The vault every registered client agrees on, if exactly one.

    Apps launched from Finder (Bindery.app) and plain shells do not inherit
    BINDERY_VAULT, but the client configs written by `install` record it.
    When they all name the same vault, that is the user's vault.
    """
    try:
        probe = Config.resolve()
        vaults = {v for v in (_registered_vault(spec) for spec in _client_targets(probe, "user").values()) if v}
    except Exception:
        return None
    return vaults.pop() if len(vaults) == 1 else None


def _vault_is_configured(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "vault", None) or os.environ.get("BINDERY_VAULT", "").strip())


def _config_from(args: argparse.Namespace) -> Config:
    command = getattr(args, "command", None)
    uses_registered_vault = (
        command in {"status", "sync", "clients", "notes", "read", "recent", "ranking"}
        or (command == "search" and getattr(args, "json", False))
    )
    if uses_registered_vault and not _vault_is_configured(args):
        agreed = _vault_from_registered_clients()
        if agreed:
            args.vault = agreed
    return Config.resolve(
        vault=getattr(args, "vault", None),
        state_dir=getattr(args, "state_dir", None),
        max_tokens=getattr(args, "max_tokens", None),
        limit=getattr(args, "limit", None),
        semantic=False if getattr(args, "no_semantic", False) else None,
        include=getattr(args, "include", None),
        exclude=getattr(args, "exclude", None),
        project=getattr(args, "project", None),
    )


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(_config_from(args))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    config = _config_from(args)
    store = Store(config.db_path)
    report = reindex(config, store, force=args.force)
    if args.embed:
        count = _embed_missing(config, store)
        print(f"embedded {count} passage(s)")
        # Catches the case where sqlite-vec was installed after the vectors
        # were: they are all in `vectors`, and the ANN index has never seen
        # them. Cheap when it is already in step.
        if store.ann_enabled:
            print(f"nearest-neighbour index: {store.rebuild_vector_index()} vector(s)")
    print(json.dumps(report.as_dict(), indent=2))
    store.close()
    return 0


def _embed_missing(config: Config, store: Store) -> int:
    """Fill in vectors for passages that do not have one yet."""
    if not config.semantic:
        return 0
    from .embed import load_backend

    backend = load_backend()
    if backend is None:
        print(
            "No embedding backend installed; keyword search remains active.\n"
            "Install one with:  uv tool install --force 'bindery-mcp[semantic]'",
            file=sys.stderr,
        )
        return 0
    from .indexer import refresh_embeddings

    done = refresh_embeddings(config, store)
    return done


def cmd_search(args: argparse.Namespace) -> int:
    if args.json:
        return cmd_dashboard(args)
    config = _config_from(args)
    store = Store(config.db_path)
    reindex(config, store)
    hits, meta = search(config, store, args.query, limit=args.limit, max_tokens=args.max_tokens)
    print(f"{meta['returned']} passage(s), ~{meta['tokens']} tokens "
          f"({meta['truncated']} dropped by budget)\n")
    for hit in hits:
        where = hit.chunk.path + (f" # {hit.chunk.breadcrumb}" if hit.chunk.breadcrumb else "")
        print(f"--- {where}  [{hit.matched_by}]")
        print(hit.chunk.body)
        print()
    store.close()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from . import dashboard

    try:
        config = _config_from(args)
        if not _vault_is_configured(args):
            raise dashboard.DashboardError("未索引")
        result = dashboard.payload(
            config,
            args.command,
            value=getattr(args, "query", getattr(args, "path", "")),
            limit=getattr(args, "limit", None),
            max_tokens=getattr(args, "max_tokens", None),
        )
        error = None
    except dashboard.DashboardError as exc:
        result, error = dashboard.EMPTY_PAYLOADS[args.command], str(exc)
    except (OSError, sqlite3.DatabaseError, ValueError, RuntimeError, OverflowError):
        result, error = dashboard.EMPTY_PAYLOADS[args.command], "Vault または索引を読み取れません"
    print(json.dumps({**result, "error": error}))
    return 0


def _read_status_db(config: Config) -> dict | None:
    """Read index health without opening Store or changing the database."""
    path = config.db_path
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "meta" not in tables:
                return {"stats": {"notes": 0, "chunks": 0, "links": 0, "vectors": 0},
                        "migration": True, "rebuild": False}
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            migration = row is None or str(row[0]) != str(SCHEMA_VERSION)
            stats: dict[str, int] = {}
            for table in ("notes", "chunks", "links", "vectors"):
                stats[table] = (
                    int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if table in tables else 0
                )
            rebuild_row = conn.execute(
                "SELECT value FROM meta WHERE key='rebuild_required'"
            ).fetchone()
            rebuild = bool(rebuild_row and str(rebuild_row[0]) == "1")
            return {"stats": stats, "migration": migration, "rebuild": rebuild}
    except (OSError, sqlite3.DatabaseError):
        return {"stats": {"notes": 0, "chunks": 0, "links": 0, "vectors": 0},
                "migration": True, "rebuild": False, "unreadable": True}


def cmd_status(args: argparse.Namespace) -> int:
    config = _config_from(args)
    explicitly_configured = _vault_is_configured(args)
    db_info = _read_status_db(config) if explicitly_configured else None
    stats = (db_info or {"stats": {"notes": 0, "chunks": 0, "links": 0, "vectors": 0}})["stats"]
    backend = None
    if not getattr(args, "json", False):
        from .embed import load_backend
        backend = (
            load_backend()
            if db_info and config.vault.exists() and not db_info.get("migration") and config.semantic
            else None
        )

    problems: list[str] = []
    if not explicitly_configured:
        problems.append("vault is not specified; index is not initialized (未索引)")
    elif not config.vault.exists():
        problems.append(f"vault does not exist: {config.vault}")
    elif db_info is None:
        problems.append(f"vault is not indexed (未索引): {config.vault}")
    elif db_info.get("unreadable"):
        problems.append(f"index cannot be read without changing it: {config.db_path}")
    elif db_info.get("migration") or db_info.get("rebuild"):
        problems.append("index needs migration or rebuild; run: bindery index")
    elif stats["notes"] == 0:
        problems.append(f"vault has no indexed Markdown notes: {config.vault}")
    if not getattr(args, "json", False) and config.semantic and db_info and not db_info.get("migration") and backend is None:
        problems.append("semantic search requested but no backend installed "
                        "(uv tool install --force 'bindery-mcp[semantic]') - keyword search still works")
    if backend is not None and stats["vectors"] < stats["chunks"]:
        problems.append(f"{stats['chunks'] - stats['vectors']} passage(s) not embedded yet "
                        "(run: bindery index --embed)")

    if getattr(args, "json", False):
        problems = list(problems)
        payload = {
            "version": __version__,
            "vault": str(config.vault),
            "vault_exists": config.vault.exists(),
            "indexed": bool(db_info and not db_info.get("migration") and not db_info.get("unreadable")),
            "notes": stats["notes"] if db_info else None,
            "passages": stats["chunks"] if db_info else None,
            "embedded": stats["vectors"] if db_info else None,
            "semantic": "configured" if config.semantic else "off (keyword only)",
            "state_dir": str(config.state_dir),
            "error": "; ".join(problems) if problems else None,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(f"bindery {__version__}")
    print(f"  vault          {config.vault}")
    print(f"  index          {config.db_path}")
    print(f"  notes          {stats['notes']}")
    print(f"  passages       {stats['chunks']}")
    print(f"  links          {stats['links']}")
    print(f"  embedded       {stats['vectors']}")
    print(f"  max tokens     {config.max_tokens}")
    from .workspace import resolve

    print(f"  project        {resolve(state_dir=config.state_dir).describe()}")
    print(f"  semantic       {backend.name if backend else 'off (keyword only)'}")
    if backend:
        print("  vector index   read-only status")
    if problems:
        print("\nissues:")
        for problem in problems:
            print(f"  ! {problem}")
    else:
        print("\nready")
    return 1 if problems else 0


def _registered_vault(spec: dict) -> str | None:
    path = Path(spec["path"])
    if not path.is_file():
        return None
    try:
        if spec["format"] == "toml":
            value = tomllib.loads(path.read_text(encoding="utf-8"))
            entry = value.get("mcp_servers", {}).get("bindery", {})
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            entry = value.get(spec["format"], {}).get("bindery", {})
        args = entry.get("args", []) if isinstance(entry, dict) else []
        if "--vault" in args:
            return str(args[args.index("--vault") + 1])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError):
        return None
    return None


_INTERPRETERS = {"node", "python", "python3", "bun", "deno", "sh", "bash", "zsh", "env"}


def _classify_executable(executable: str, command: str = "") -> str:
    """Name the agent from a process's executable, never from its arguments.

    Matching words anywhere in the command line misreads prompts: an agent
    asked about "bindery" or "claude" would be attributed to the wrong client.
    When the executable is an interpreter (an npm-installed CLI runs as node),
    the script it runs is the program, so look at that path instead.
    """
    name = Path(executable).name.lower()
    if name in _INTERPRETERS or name.startswith("python"):
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        script = next((part for part in parts[1:] if not part.startswith("-")), "")
        name = Path(script).name.lower()
    if name in {"agy", "antigravity"} or name.startswith("antigravity"):
        return "antigravity"
    if name in {"claude", "claude-code"} or name.startswith("claude"):
        return "claude"
    if name.startswith("codex"):
        return "codex"
    return "unknown"


def _process_vault(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    try:
        return parts[parts.index("--vault") + 1]
    except (ValueError, IndexError):
        return None


def _connections() -> list[dict]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,command="],
            capture_output=True, text=True, check=False, timeout=5,
        )
        # `comm` is the executable path alone, which may contain spaces
        # (".../Application Support/..."), so it cannot be cut out of `command`.
        names = subprocess.run(
            ["ps", "-axo", "pid=,comm="],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    executables: dict[int, str] = {}
    for line in names.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            executables[int(parts[0])] = parts[1]
    rows: dict[int, dict] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.{24})\s+(.*)", line)
        if not match:
            continue
        pid, ppid = int(match.group(1)), int(match.group(2))
        started = None
        try:
            started = _dt.datetime.strptime(match.group(3), "%a %b %d %H:%M:%S %Y").replace(tzinfo=_dt.datetime.now().astimezone().tzinfo).isoformat()
        except ValueError:
            pass
        rows[pid] = {"pid": pid, "ppid": ppid, "command": match.group(4), "started_at": started}
    found: list[dict] = []
    for row in rows.values():
        if "bindery" not in row["command"] or "serve" not in row["command"]:
            continue
        current = row
        client = "unknown"
        visited: set[int] = set()
        current = rows.get(row["ppid"])  # the server itself is never the client
        while current and current["pid"] not in visited:
            visited.add(current["pid"])
            executable = executables.get(current["pid"], current["command"].split(" ", 1)[0])
            detected = _classify_executable(executable, current["command"])
            if detected != "unknown":
                client = detected
                break
            current = rows.get(current["ppid"])
        found.append({"client": client, "pid": row["pid"], "started_at": row["started_at"], "vault": _process_vault(row["command"])})
    return sorted(found, key=lambda item: item["pid"])


def cmd_clients(args: argparse.Namespace) -> int:
    config = _config_from(args)
    targets = _client_targets(config, "user")
    clients = []
    for client_id, spec in sorted(targets.items()):
        # The JSON contract uses the canonical IDs for the three primary
        # clients, while account-specific Codex profiles remain visible.
        canonical = client_id.split(":", 1)[0]
        clients.append({
            "id": canonical,
            "label": spec["label"],
            "detected": canonical in _detect_clients() or client_id in _detect_clients(),
            "registered": _registered_vault(spec) is not None,
            "config_path": str(spec["path"]),
            "vault": _registered_vault(spec),
        })
    payload = {"clients": clients, "connections": _connections(), "error": None}
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _sync_result_output(result, *, json_output: bool) -> int:
    status = dict(result.status or {})
    if result.exit_code:
        error = status.get("error") or status.get("last_error") or status.get("blocked_reason") or result.message
        if error:
            error = " ".join(str(error).split())
            status["error"] = error
            print(f"bindery sync: {error}", file=sys.stderr)
    if json_output:
        print(json.dumps({"exit_code": result.exit_code, "status": status}, ensure_ascii=False))
        return result.exit_code
    if result.message:
        print(result.message)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return result.exit_code


def cmd_sync(args: argparse.Namespace) -> int:
    from . import sync

    config = _config_from(args)
    action = getattr(args, "sync_action", "run")
    if action == "status":
        json_output = getattr(args, "json", False)
        payload = sync.sync_status(config, include_paths=json_output)
        if json_output:
            print(json.dumps(payload))
            return 0
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if action == "init":
        result = sync.init_sync(
            config,
            args.remote,
            write=args.write,
            allow_public_host=args.allow_public_host,
            key_store=getattr(args, "key_store", None),
        )
        return _sync_result_output(result, json_output=getattr(args, "json", False))
    if action == "pair":
        result = sync.pair(
            config,
            args.host,
            replace=args.replace,
            remote_vault=args.remote_vault,
            remote_bindery=getattr(args, "remote_bindery", None),
            remote_key_store=getattr(args, "remote_key_store", None),
            key_store=getattr(args, "key_store", None),
            peer_remote=getattr(args, "peer_remote", None),
        )
        return _sync_result_output(result, json_output=getattr(args, "json", False))
    if action == "accept-key":
        result = sync.accept_key(
            config,
            args.sync_id,
            sys.stdin.buffer.read(),
            replace=args.replace,
            key_store=getattr(args, "key_store", None),
        )
        return _sync_result_output(result, json_output=getattr(args, "json", False))
    if action == "key-export":
        try:
            print(sync.key_export(config))
            return 0
        except Exception as exc:
            print(f"! {exc}", file=sys.stderr)
            return 1
    if action == "install":
        json_output = getattr(args, "json", False)
        result = sync.install_scheduler(config, args.interval, write=args.write, emit=not json_output)
        return _sync_result_output(result, json_output=json_output)
    if action == "uninstall":
        json_output = getattr(args, "json", False)
        result = sync.uninstall_scheduler(config, write=args.write, emit=not json_output)
        return _sync_result_output(result, json_output=json_output)
    if action == "rekey":
        result = sync.rekey(
            config,
            write=args.write,
            purge_remote=args.purge_remote,
            key_store=getattr(args, "key_store", None),
        )
        return _sync_result_output(result, json_output=getattr(args, "json", False))
    result = sync.run_sync(
        config,
        allow_public_host=getattr(args, "allow_public_host", False),
        key_store=getattr(args, "key_store", None),
        include_paths=getattr(args, "json", False),
    )
    return _sync_result_output(result, json_output=getattr(args, "json", False))


def cmd_review(args: argparse.Namespace) -> int:
    """Human-facing view of the growth loop."""
    from .growth import find_duplicates, hot_paths, knowledge_gaps, promotion_candidates, stale_notes

    config = _config_from(args)
    store = Store(config.db_path)
    reindex(config, store)

    gaps = knowledge_gaps(store, min_count=args.min_count)
    hot = hot_paths(store)
    duplicates = find_duplicates(store)
    stale = stale_notes(store)
    promote = promotion_candidates(store)

    if args.json:
        print(json.dumps({
            "knowledge_gaps": [{"query": g.query, "asked": g.count} for g in gaps],
            "load_bearing_notes": [{"path": p, "retrievals": u, "weight": round(w, 3)} for p, u, w in hot],
            "near_duplicates": [{"a": d.left, "b": d.right, "similarity": d.similarity} for d in duplicates],
            "stale_notes": [{"path": p, "age_days": a} for p, a in stale],
            "promotion_candidates": [{"tag": c.tag, "journal_entries": c.entries} for c in promote],
        }, ensure_ascii=False, indent=2))
        store.close()
        return 0

    def section(title, rows, render, total=None):
        shown = len(rows)
        count = total if total is not None else shown
        suffix = f" - showing {shown} of {count}" if count > shown else ""
        print(f"\n{title}{suffix}")
        if not rows:
            print("  (none)")
            return
        for row in rows:
            print("  " + render(row))

    print("bindery review")
    section("knowledge gaps (asked, never answered)", gaps,
            lambda g: f"{g.count}x  {g.query}")
    section("load-bearing notes", hot,
            lambda r: f"{r[1]:>4} reads  weight {r[2]:.2f}  {r[0]}")
    section("shown but never read", store.shown_but_unread(),
            lambda r: f"{r[1]:>4} times shown  {r[0]}")
    section("near-duplicates", duplicates[:10],
            lambda d: f"{d.similarity:.2f}  {d.left}  <->  {d.right}",
            total=len(duplicates))
    section("stale notes (never retrieved)", stale,
            lambda r: f"{r[1]:>7.0f}d  {r[0]}")
    section("promotion candidates (recurring journal topics)", promote,
            lambda c: f"{c.entries}x  #{c.tag}")
    store.close()
    return 0


#: The instruction block that closes the growth loop.
#:
#: An MCP server cannot make an agent record anything - it can only answer
#: calls. These sentences are therefore the entire mechanism by which learning
#: gets written down, which is why they ship as a first-class artifact rather
#: than as an example buried in the README. The same text works verbatim in
#: CLAUDE.md and AGENTS.md; neither agent is the primary one.
AGENT_INSTRUCTIONS = """\
## Shared memory (Bindery)

You share a persistent memory with the other coding agents on this machine.
It is not scratch space - what you write, the next session reads.

- **Before starting work**, call `memory_search` for prior decisions on the
  topic. Do this even when you think you know the answer; a past decision
  overrides a fresh guess.
- Searches are **scoped to the current project** by default, plus notes marked
  as applying everywhere. If the answer might not be project-specific - a
  language idiom, a tool's behaviour, a workflow preference - repeat the search
  with `scope="all"`. The result will tell you when matches exist elsewhere.
  Treat a decision from another repository as context, never as this project's
  decision.
- **When you learn something the next session would otherwise rediscover**,
  call `memory_learn` immediately - do not wait until the end. Record it when
  any of these is true:
  - you made a design decision, or rejected an alternative, and why
  - you found a constraint that is not obvious from the code
  - a fix worked and the reason was not self-evident
  Always pass `tags`. Tags are what let a recurring topic graduate into a
  durable note of its own.
- **Record what failed, not only what worked.** A dead end is worth more than
  a success, because nothing in the repository records it and the next session
  will otherwise spend the same hours on it. Write down what was attempted,
  the error or behaviour you actually observed, the root cause if you found
  one, and what you did instead.
- **When a decision changes, update the note that stated it** - do not add a
  second note saying the opposite. Search first, edit the existing durable
  note with `memory_write`, and let the journal keep the history. Two live
  notes disagreeing is how a memory starts giving wrong answers confidently.
- **Never record credentials, tokens, keys, or personal data**, even in
  passing, and even inside a quoted command or error message.
- **Do not** record routine progress, restatements of the code, or anything
  you could recover by reading the repository. Noise costs the same tokens as
  signal and crowds it out.
- Use `memory_write` for durable reference notes, `memory_learn` for the
  running record of what a session figured out.
- Both record the current project automatically. Pass `project=""` when what
  you learned is true regardless of which codebase you are in.
- Finished sessions are captured automatically, so nothing is lost if you
  forget - but a captured transcript is a record of what happened, not a
  conclusion. Results marked as coming from a session are weaker evidence than
  a note somebody wrote, and should be confirmed before being acted on.
"""


def _server_command() -> tuple[str, list[str]]:
    """How an agent should start *this* installation of the server.

    Asking PATH for ``bindery`` is not the same question. PATH can resolve to a
    different copy than the one now running - an activated project virtualenv,
    or one left behind by an earlier install elsewhere - and the agents would
    then be pointed at a server the user did not just install, or at a path
    that disappears when that virtualenv does. The console script sitting
    beside the running interpreter is unambiguous, so prefer it, and fall back
    to ``python -m`` for installs that have no script at all.
    """
    beside = Path(sys.executable).parent / "bindery"
    if not beside.exists():
        beside = beside.with_suffix(".exe")
    found = shutil.which("bindery")
    if beside.exists():
        # Same file reached two ways: prefer the PATH name, which is the
        # stable one users and uv keep pointing at across upgrades.
        if found and Path(found).resolve() == beside.resolve():
            return found, []
        return str(beside), []
    if found:
        return found, []
    return sys.executable, ["-m", "bindery"]


def _client_targets(config: Config, scope: str = "user", pinned_project: str | None = None) -> dict[str, dict]:
    """Config payloads for every supported client, keyed by client id.

    Scope matters and clients express it differently. Codex keeps MCP servers
    in one global ``config.toml``; Claude Code and Antigravity have both
    project and user scopes. User scope is the default so the memory works
    across projects, and project scope is opt-in where the client supports it.
    """
    command, extra = _server_command()
    args = [*extra, "--vault", str(config.vault)]
    # The project is decided per session, from the directory the agent starts
    # in. Writing the name detected where `install` happened to run would pin
    # every future session to that one project, so it is written only when
    # the user named it on the command line.
    if pinned_project is not None:
        args.extend(("--project", pinned_project))
    claude_path = (
        Path.cwd() / ".mcp.json" if scope == "project" else Path.home() / ".claude.json"
    )
    antigravity_path = (
        Path.cwd() / ".agents" / "mcp_config.json"
        if scope == "project"
        else Path.home() / ".gemini" / "config" / "mcp_config.json"
    )
    base = _with_index_boundaries({
        "command": command, "launch_args": args, "vault": str(config.vault),
        "lock_dir": config.state_dir,
    }, {"include": config.include, "exclude": config.exclude})
    targets: dict[str, dict] = {
        "claude": {
            "label": "Claude Code (app, CLI, and VS Code extension)",
            # All three read this same file, so one entry covers them.
            "path": claude_path,
            "scope": scope,
            "format": "mcpServers",
            **base,
        },
        "antigravity": {
            "label": "Antigravity CLI",
            # Antigravity keeps MCP servers in a standalone JSON profile.
            "path": antigravity_path,
            "scope": scope,
            "format": "mcpServers",
            **base,
        },
        "codex": {
            "label": "Codex (CLI, IDE extension, app)",
            # Codex reads MCP servers from one global file; there is no
            # per-project equivalent to fall back to.
            "path": Path.home() / ".codex" / "config.toml",
            "scope": "user",
            "format": "toml",
            **base,
        },
        "vscode": {
            # GitHub Copilot's agent mode, and anything else using VS Code's
            # own MCP support. A different schema from Claude's: servers live
            # under `servers` and each declares its transport.
            "label": "VS Code (Copilot agent mode)",
            "path": _vscode_mcp_path(),
            "scope": "user",
            "format": "servers",
            **base,
        },
        "cursor": {
            "label": "Cursor",
            "path": Path.home() / ".cursor" / "mcp.json",
            "scope": "user",
            "format": "mcpServers",
            **base,
        },
    }
    for home in _codex_account_homes():
        # codex-multi gives each account its own CODEX_HOME, and a server
        # registered in ~/.codex is invisible from all of them. Skipping these
        # is how background Codex runs end up with no shared memory at all -
        # which is the one thing this project exists to provide.
        targets[f"codex:{home.name}"] = {
            "label": f"Codex account '{home.name}'",
            "path": home / "config.toml",
            "scope": "user",
            "format": "toml",
            **base,
        }
    return targets


def _with_index_boundaries(spec: dict, boundaries: dict[str, list[str]]) -> dict:
    args = list(spec["launch_args"])
    env = {"BINDERY_VAULT": spec["vault"]}
    # Some clients only apply one of the two forms when starting the server.
    for name, prefixes in boundaries.items():
        for prefix in prefixes:
            args.extend((f"--{name}", prefix))
        if prefixes:
            env[f"BINDERY_{name.upper()}"] = ",".join(prefixes)
    return {**spec, "args": [*args, "serve"], "env": env, "boundaries": boundaries}


def _inherit_index_boundaries(spec: dict, entry: object, inherit: set[str]) -> dict:
    if not inherit or not isinstance(entry, dict):
        return spec
    before_serve: dict[str, list[str]] = {}
    options: dict[str, list[str]] = {}
    args = entry.get("args")
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        remaining = iter(args)
        for arg in remaining:
            flag, equals, value = arg.partition("=")
            if flag == "serve":
                before_serve, options = options, {}
            elif flag in {"--include", "--exclude", "--vault", "--project", "--state-dir", "-m"}:
                value = value if equals else next(remaining, None)
                if flag in {"--include", "--exclude"} and value is not None:
                    options.setdefault(flag[2:], []).append(value)
    # The CLI prefers args over env, and options after `serve` override the
    # same options before it. Carry forward the effective settings.
    options = before_serve | options
    env = entry.get("env")
    boundaries = dict(spec["boundaries"])
    for name in inherit:
        value = env.get(f"BINDERY_{name.upper()}") if isinstance(env, dict) else None
        if name in options:
            boundaries[name] = _clean_prefixes(options[name])
        elif isinstance(value, str):
            boundaries[name] = _clean_prefixes(value.split(","))
    return _with_index_boundaries(spec, boundaries)


def _vscode_mcp_path() -> Path:
    """Where VS Code keeps user-level MCP servers, per platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    if sys.platform == "win32":  # pragma: no cover - not exercised on CI
        return Path(os.environ.get("APPDATA", Path.home())) / "Code" / "User" / "mcp.json"
    return Path.home() / ".config" / "Code" / "User" / "mcp.json"


def _codex_account_homes() -> list[Path]:
    """Extra CODEX_HOME directories, as created by codex-multi.

    Each is a separate Codex account with its own config, so each needs its
    own registration. Sorted so that output is stable.
    """
    root = Path.home() / ".codex-homes"
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if child.is_dir())


def _detect_clients() -> set[str]:
    """Which agents look installed. Absence is a hint, never a blocker."""
    found: set[str] = set()
    if shutil.which("claude") or (Path.home() / ".claude").exists():
        found.add("claude")
    if shutil.which("agy") or (Path.home() / ".gemini" / "antigravity-cli").exists():
        found.add("antigravity")
    if shutil.which("codex") or (Path.home() / ".codex").exists():
        found.add("codex")
    if _vscode_mcp_path().parent.is_dir():
        found.add("vscode")
    if (Path.home() / ".cursor").is_dir():
        found.add("cursor")
    for home in _codex_account_homes():
        found.add(f"codex:{home.name}")
    return found


def _entry_for(spec: dict) -> dict:
    """The server entry itself, in whichever shape this client expects."""
    entry = {
        "command": spec["command"],
        "args": spec["args"],
        "env": spec["env"],
    }
    if spec["format"] == "servers":
        # VS Code requires the transport to be named explicitly.
        return {"type": "stdio", **entry}
    return entry


def _render_json(spec: dict) -> str:
    return json.dumps({spec["format"]: {"bindery": _entry_for(spec)}}, ensure_ascii=False, indent=2)


def _render_claude(spec: dict) -> str:
    return _render_json(spec)


def _render_codex(spec: dict) -> str:
    def toml_string(value: str) -> str:
        # TOML basic strings use the same escapes as JSON for the characters
        # that can occur in paths and command arguments.
        return json.dumps(str(value), ensure_ascii=False)

    arg_list = ", ".join(toml_string(a) for a in spec["args"])
    lines = [
        "[mcp_servers.bindery]",
        f"command = {toml_string(spec['command'])}",
        f"args = [{arg_list}]",
        "",
        "[mcp_servers.bindery.env]",
    ]
    lines += [f"{key} = {toml_string(value)}" for key, value in spec["env"].items()]
    return "\n".join(lines)


def _write_json(spec: dict, inherit: set[str]) -> tuple[str, dict]:
    """Merge into a JSON config, preserving everything else.

    ``~/.claude.json`` is not a config file the user wrote - it is live
    application state, tens of kilobytes of it. So this reads, adds one key
    under the client's server map, and writes the whole document back
    unchanged otherwise, after taking a backup. The same care applies to
    VS Code's and Cursor's files, which hold other people's servers.
    """
    path: Path = spec["path"]
    replaced = False
    applied = spec

    def transform(raw: str) -> str:
        nonlocal replaced, applied
        existing: dict = {}
        if raw.strip():
            try:
                existing = json.loads(raw)
            except ValueError as exc:
                raise ConfigWriteError(f"{path} is not valid JSON - left untouched.") from exc
            if not isinstance(existing, dict):
                raise ConfigWriteError(f"{path} is not a JSON object - left untouched.")
        servers = existing.setdefault(spec["format"], {})
        if not isinstance(servers, dict):
            raise ConfigWriteError(
                f"{path} has a non-object {spec['format']!r} - left untouched."
            )
        replaced = "bindery" in servers
        applied = _inherit_index_boundaries(spec, servers.get("bindery"), inherit)
        servers["bindery"] = _entry_for(applied)
        return json.dumps(existing, ensure_ascii=False, indent=2) + "\n"

    atomic_config_update(
        path,
        transform,
        lock_dir=spec["lock_dir"],
    )
    verb = "updated" if replaced else "wrote"
    return f"{verb} {path} ({spec['scope']} scope)", applied


def _write_claude(spec: dict) -> str:
    return _write_json(spec, set())[0]


def _write_target(spec: dict, inherit: set[str]) -> tuple[str, dict]:
    """Apply one client's configuration, in whatever format it uses."""
    return _write_codex(spec, inherit) if spec["format"] == "toml" else _write_json(spec, inherit)


def _render_target(spec: dict) -> str:
    return _render_codex(spec) if spec["format"] == "toml" else _render_json(spec)


#: A TOML table header, either ``[table]`` or ``[[array.of.tables]]``.
_TOML_HEADER = re.compile(r"^\s*\[\[?([^\[\]]*)\]\]?\s*$")


def _owns_codex_table(header: str) -> bool:
    """Is this table one that `install` wrote, and may therefore rewrite?

    Only ``mcp_servers.bindery`` and its subtables. Quotes are stripped because
    TOML lets a key be written bare or quoted and both name the same table.
    """
    name = "".join(header.split()).replace('"', "").replace("'", "")
    return name == "mcp_servers.bindery" or name.startswith("mcp_servers.bindery.")


def _drop_codex_block(current: str) -> tuple[str, bool]:
    """Remove our own tables from a Codex config, leaving every other one.

    Splitting on table headers rather than parsing and re-emitting TOML is
    deliberate: this file belongs to the user, and a round trip through a
    parser would silently reformat their comments and quoting.
    """
    kept: list[str] = []
    dropping = False
    found = False
    for line in current.splitlines(keepends=True):
        header = _TOML_HEADER.match(line)
        if header:
            dropping = _owns_codex_table(header.group(1))
            found = found or dropping
        if not dropping:
            kept.append(line)
    return "".join(kept), found


def _write_codex(spec: dict, inherit: set[str]) -> tuple[str, dict]:
    """Write the server block, replacing an earlier one rather than duplicating it.

    Leaving an existing block untouched was the safer-looking choice and the
    wrong one: `bindery` moves when it is reinstalled elsewhere, and a stale
    ``command`` path means Codex silently loses the memory while the config
    still looks configured. The block is ours, keyed by our own name, so we
    replace it; anything else in the file is copied through untouched.
    """
    path: Path = spec["path"]
    replaced = False
    applied = spec

    def transform(current: str) -> str:
        nonlocal replaced, applied
        remainder, replaced = _drop_codex_block(current)
        if inherit and replaced:
            try:
                servers = tomllib.loads(current).get("mcp_servers", {})
            except ValueError as exc:
                raise ConfigWriteError(f"{path} is not valid TOML - left untouched.") from exc
            applied = _inherit_index_boundaries(spec, servers.get("bindery"), inherit)
        else:
            applied = spec
        remainder = remainder.rstrip("\n")
        separator = "\n\n" if remainder else ""
        return remainder + separator + _render_codex(applied) + "\n"

    atomic_config_update(path, transform, lock_dir=spec["lock_dir"])
    return (f"updated {path}" if replaced else f"wrote {path}"), applied


#: Where each agent reads standing instructions from.
#:
#: Project scope covers one repository; user scope covers every session on the
#: machine. A memory shared across projects wants user scope - installing per
#: project means repeating the step in every checkout and forgetting it in most
#: of them, and an agent that was never told to record anything records nothing.
PROMPT_TARGETS = {
    "project": [Path("AGENTS.md"), Path("CLAUDE.md")],
    "user": [
        Path(".codex") / "AGENTS.md",
        Path(".claude") / "CLAUDE.md",
        Path(".gemini") / "GEMINI.md",
    ],
}


#: Markers around the block Bindery owns, so that improving the instructions
#: reaches installs that already have an older version. Without them the only
#: safe move was to leave an existing block alone - which meant every
#: improvement to the one mechanism that makes anything get recorded applied
#: to new users only.
BLOCK_BEGIN = "<!-- bindery:begin - managed by `bindery prompt`, edits will be replaced -->"
BLOCK_END = "<!-- bindery:end -->"

#: The heading of blocks written before the markers existed.
LEGACY_HEADING = "## Shared memory (Bindery)"


def _managed_block() -> str:
    return f"{BLOCK_BEGIN}\n{AGENT_INSTRUCTIONS.strip()}\n{BLOCK_END}\n"


def _apply_instructions(current: str) -> tuple[str, str]:
    """Insert or refresh the block, leaving everything around it alone.

    Returns the new text and what happened, so the caller can say so.
    """
    block = _managed_block()
    if BLOCK_BEGIN in current and BLOCK_END in current:
        start = current.index(BLOCK_BEGIN)
        end = current.index(BLOCK_END) + len(BLOCK_END)
        tail = current[end:].lstrip("\n")
        rebuilt = current[:start] + block + ("\n" + tail if tail else "")
        return rebuilt, "unchanged" if rebuilt == current else "updated"

    if LEGACY_HEADING in current:
        # Written before the markers. The block runs to the next heading of
        # the same level, or to the end of the file - it contains none of its
        # own, so this cannot swallow anything that was not ours.
        start = current.index(LEGACY_HEADING)
        rest = current[start + len(LEGACY_HEADING):]
        offset = rest.find("\n## ")
        end = len(current) if offset == -1 else start + len(LEGACY_HEADING) + offset + 1
        tail = current[end:].lstrip("\n")
        rebuilt = current[:start] + block + ("\n" + tail if tail else "")
        return rebuilt, "updated"

    prefix = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
    return current + prefix + block, "added"


def cmd_prompt(args: argparse.Namespace) -> int:
    """Emit the agent instructions that make the memory actually accumulate."""
    if not args.write:
        print(AGENT_INSTRUCTIONS)
        return 0

    scope = "user" if args.user else "project"
    root = Path.home() if args.user else Path.cwd()
    lock_dir = Config.resolve().state_dir
    written: list[str] = []
    failed = False
    for relative in PROMPT_TARGETS[scope]:
        target = root / relative
        if not target.parent.exists():
            if (
                scope == "user"
                and relative == Path(".gemini") / "GEMINI.md"
                and "antigravity" in _detect_clients()
            ):
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                print(f"- {target}: parent directory missing - skipped.")
                continue
        action = "unchanged"

        def transform(current: str) -> str:
            nonlocal action
            updated, action = _apply_instructions(current)
            return updated

        try:
            atomic_config_update(target, transform, lock_dir=lock_dir)
        except (ConcurrentUpdate, ConfigWriteError, OSError, UnicodeError) as exc:
            print(f"- {target}: failed ({exc})", file=sys.stderr)
            failed = True
            continue
        if action == "unchanged":
            print(f"- {target}: already up to date.")
            continue
        written.append(f"{target} ({action})")

    if written:
        print("appended to:")
        for path in written:
            print(f"  {path}")
    else:
        print("nothing written.")
    return 1 if failed else 0


def cmd_project(args: argparse.Namespace) -> int:
    """Inspect and edit which directories count as which project.

    Exists because the alternative - inferring it from the directory name -
    silently splits one project into several depending on where the editor was
    opened, and nothing in the output ever says that is what happened.
    """
    from .workspace import MARKER, load_registry, resolve, save_registry

    config = _config_from(args)
    state_dir = config.state_dir
    action = args.action or "list"

    if action == "which":
        # `which PATH` puts the path in the NAME slot, since the positionals
        # are shared with `add NAME PATH`. Accept it from either.
        given = args.path or args.name
        target = Path(given).expanduser() if given else Path.cwd()
        print(f"{target}")
        print(f"  project: {resolve(target, state_dir=state_dir).describe()}")
        return 0

    if action == "list":
        entries = load_registry(state_dir)
        if not entries:
            print("No registered workspaces. Every directory falls back to git or its own name.")
        for entry in entries:
            print(f"  {entry['name']:<24} {entry['path']}")
        here = resolve(Path.cwd(), state_dir=state_dir)
        print(f"\nhere: {Path.cwd()}\n  project: {here.describe()}")
        return 0

    if action == "add":
        if not args.name:
            print("usage: bindery project add NAME [PATH]", file=sys.stderr)
            return 2
        target = Path(args.path).expanduser() if args.path else Path.cwd()
        target = target.resolve()
        if not target.is_dir():
            print(f"not a directory: {target}", file=sys.stderr)
            return 2
        entries = [e for e in load_registry(state_dir) if Path(e["path"]) != target]
        entries.append({"name": args.name, "path": str(target)})
        save_registry(state_dir, entries)
        print(f"{args.name} -> {target}")
        if args.marker:
            marker = target / MARKER
            atomic_write(marker, args.name + "\n")
            print(f"wrote {marker}")
        return 0

    if action == "remove":
        entries = load_registry(state_dir)
        kept = [
            e for e in entries
            if e["name"] != args.name and Path(e["path"]) != Path(args.name).expanduser()
        ]
        if len(kept) == len(entries):
            print(f"nothing registered as {args.name!r}", file=sys.stderr)
            return 1
        save_registry(state_dir, kept)
        print(f"removed {args.name}")
        return 0

    print(f"unknown action: {action}", file=sys.stderr)
    return 2


def cmd_install(args: argparse.Namespace) -> int:
    """Print - or apply - client configuration.

    With no client named, every supported client is emitted, so no one client
    is treated as the primary one.
    """
    config = _config_from(args)
    scope = "project" if getattr(args, "local", False) else "user"
    targets = _client_targets(config, scope, pinned_project=getattr(args, "project", None))
    if args.client and args.client not in targets:
        print(f"Unknown client {args.client!r}. Known: {', '.join(sorted(targets))}", file=sys.stderr)
        return 2
    chosen = [args.client] if args.client else sorted(targets)
    detected = _detect_clients()
    failed = False
    inherit = {name for name in ("include", "exclude") if getattr(args, name, None) is None}

    for index, client in enumerate(chosen):
        spec = targets[client]
        if index:
            print()
        if client not in detected and not args.client:
            # Writing a config for an agent that is not installed leaves
            # litter for something the user may never run. Naming the client
            # explicitly still forces it, for installing ahead of the agent.
            print(f"# {spec['label']} - not installed on this machine, skipped")
            continue
        print(f"# {spec['label']} - {spec['scope']} scope - {spec['path']}")
        print()
        if args.write:
            try:
                message, applied = _write_target(spec, inherit)
                print(_render_target(applied))
                print(f"\n{message}")
            except (ConfigWriteError, ConcurrentUpdate, OSError, UnicodeError) as exc:
                print(f"! failed to update {spec['path']}: {exc}", file=sys.stderr)
                failed = True
        else:
            print(_render_target(spec))

    print()
    print("# Point every client at the SAME BINDERY_VAULT - that is what shares the memory.")
    print(f"# Vault: {config.vault}")
    print("#")
    print("# This command configures the servers. It does NOT teach the agents to use them -")
    print("# nothing is ever recorded unless the agents are told to record it:")
    print("#   bindery prompt --global --write")
    print("#")
    print("# Or do the whole thing at once:")
    print("#   bindery setup --write")
    return 1 if failed else 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Do every setup step in one pass, and prove it worked.

    The measure of this command is that nothing is left for the user to decide
    afterwards: no retrieval mode to pick, no embedding model to fetch, no
    per-agent configuration to repeat, and no way to end up with a working
    install that quietly retrieves worse than the one in the README.

    Defaults to a dry run, because the instructions step edits the agent policy
    files in your home directory - usually long and hand-maintained.
    """
    config = _config_from(args)
    mode = "APPLYING" if args.write else "DRY RUN - nothing will be written"
    print(f"bindery setup ({mode})")
    print(f"  vault:   {config.vault}")
    print(f"  project: {resolve_project(state_dir=config.state_dir).describe()}")

    steps = 5
    print(f"\n[1/{steps}] semantic search")
    if not config.semantic:
        print("  turned off (BINDERY_SEMANTIC=0 or --no-semantic) - keyword search only")
    elif args.write:
        _prepare_semantic(config)
    else:
        print("  would download the embedding model on first use (~220MB, once)")

    print(f"\n[2/{steps}] index")
    store = None
    if args.write:
        store = Store(config.db_path)
        report = reindex(config, store)
        print(f"  {report.scanned} note(s) scanned, {report.added} added, {report.updated} updated")
        embedded = _embed_missing(config, store)
        if embedded:
            print(f"  {embedded} passage(s) embedded")
        if store.ann_enabled:
            store.rebuild_vector_index()
    else:
        print(f"  would index {config.vault}")

    print(f"\n[3/{steps}] session capture")
    if args.write:
        seen = set_baseline(config)
        print(f"  {seen} existing session(s) marked as history and left alone")
        print("  new sessions will be captured as they finish")
    else:
        print("  would record existing sessions as a baseline and capture only new ones")

    print(f"\n[4/{steps}] MCP server configuration")
    targets = _client_targets(config, "project" if args.local else "user",
                              pinned_project=getattr(args, "project", None))
    detected = _detect_clients()
    config_failed = False
    for client in sorted(targets):
        spec = targets[client]
        if client not in detected:
            # Creating a config file for an agent that is not installed leaves
            # litter in a home directory for something the user may never use.
            # Naming the client explicitly still forces it.
            print(f"  {spec['label']}: not installed - skipped")
            continue
        if args.write:
            try:
                message, _ = _write_target(spec, set())
                print(f"  {spec['label']}: {message}")
                if isinstance(message, str) and message.startswith("!"):
                    config_failed = True
            except (ConfigWriteError, ConcurrentUpdate, OSError, UnicodeError) as exc:
                print(f"  {spec['label']}: failed ({exc})", file=sys.stderr)
                config_failed = True
        else:
            print(f"  {spec['label']}: would write {spec['path']} [{spec['scope']} scope]")

    print(f"\n[5/{steps}] agent instructions (this is the step that makes anything get recorded)")
    prompt_status = 0
    if args.write:
        prompt_status = cmd_prompt(argparse.Namespace(write=True, user=True))
    else:
        for relative in PROMPT_TARGETS["user"]:
            target = Path.home() / relative
            state = "exists" if target.exists() else "would be created"
            print(f"  would append to {target} ({state})")

    if not args.write:
        print("\nRe-run with --write to apply. Existing files are backed up, never replaced.")
        return 0

    ok = _health_check(config, store)
    if store is not None:
        store.close()
    print("\nRestart your agents so they pick up the new configuration.")
    return 0 if ok and not config_failed and prompt_status == 0 else 1


def _prepare_semantic(config: Config) -> None:
    """Fetch the embedding model now rather than during the first search.

    Downloading it lazily would mean the first question an agent asks takes a
    couple of minutes, with no indication of why.
    """
    from .embed import load_backend

    print("  fetching the embedding model if it is not already cached…")
    backend = load_backend()
    if backend is None:
        print("  ! no embedding backend available - keyword search will still work")
        return
    print(f"  {backend.name}: {getattr(backend, 'model_name', 'ready')} ({backend.dim} dims)")


def _health_check(config: Config, store: Store | None) -> bool:
    """Prove the thing works, rather than reporting that it was configured.

    Writing a note and finding it again exercises the whole path - locking,
    atomic write, indexing, embedding, and retrieval - which is the only claim
    worth making at the end of an install.
    """
    print("\nchecking:")
    owned = store is None
    store = store or Store(config.db_path)
    problems: list[str] = []

    # journal/ is always indexed, allowlist or not - it is Bindery's own area.
    probe_rel = "journal/.bindery-healthcheck.md"
    probe = config.vault / probe_rel
    try:
        update_text(
            probe,
            lambda _c: "---\nproject: \n---\n\n# health check\n\n"
                       "bindery-healthcheck-token 疎通確認\n",
            lock_dir=config.state_dir,
        )
        index_path(config, store, probe)
        hits, _ = search(config, store, "bindery-healthcheck-token", learn=False, scope="all")
        print(f"  write and retrieve   {'ok' if hits else 'FAILED'}")
        if not hits:
            problems.append("wrote a note but could not retrieve it")
    except Exception as exc:
        print(f"  write and retrieve   FAILED ({type(exc).__name__})")
        problems.append(str(exc))
    finally:
        probe.unlink(missing_ok=True)
        store.delete_note(probe_rel)
        store.commit()

    from .embed import load_backend

    backend = load_backend() if config.semantic else None
    print(f"  semantic search      {backend.name if backend else 'off (keyword only)'}")
    print(f"  vector index         {'sqlite-vec' if store.ann_enabled else 'exact scan'}")

    stats = store.stats()
    unembedded = stats["chunks"] - stats["vectors"]
    if backend and unembedded > 0:
        print(f"  ! {unembedded} passage(s) not embedded - run: bindery index --embed")

    # The agents launch the server by absolute path, so they work regardless -
    # but a human whose PATH is missing the directory gets "command not found"
    # from every instruction in the README, with nothing explaining why.
    on_path = shutil.which("bindery")
    installed = Path(sys.executable).parent / "bindery"
    if not on_path and installed.exists():
        print(f"  ! `bindery` is not on your PATH. Add: export PATH=\"{installed.parent}:$PATH\"")
        print("    Agents are unaffected - they use the full path.")

    clients = sorted(_detect_clients())
    print(f"  agents configured    {', '.join(clients) if clients else 'none detected'}")
    print(f"  indexed              {stats['notes']} note(s), {stats['chunks']} passage(s)")

    if owned:
        store.close()
    if problems:
        for problem in problems:
            print(f"  ! {problem}")
        print("\nBindery is NOT ready.")
        return False
    print("\nBindery is ready. Memory will now maintain itself.")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bindery",
        description="One shared memory for Claude Code, Codex, and Antigravity CLI.",
    )
    parser.add_argument("--version", action="version", version=f"bindery {__version__}")
    # Server command lines generated for clients place the resolved boundary
    # before the subcommand. Keep the same options available after the
    # subcommand for the documented CLI form as well.
    _add_common(parser)
    _add_project(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the MCP server on stdio (what agents launch).")
    _add_common(p_serve, suppress_defaults=True)
    _add_project(p_serve, suppress_defaults=True)
    p_serve.add_argument("--max-tokens", type=int, help="Default response budget.")
    p_serve.add_argument("--limit", type=int, help="Default passages per search.")
    p_serve.set_defaults(func=cmd_serve)

    p_index = sub.add_parser("index", help="Scan the vault and refresh the index.")
    _add_common(p_index, suppress_defaults=True)
    _add_project(p_index, suppress_defaults=True)
    p_index.add_argument("--force", action="store_true", help="Reindex every note.")
    p_index.add_argument("--embed", action="store_true", help="Also compute missing embeddings.")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search from the terminal.")
    _add_common(p_search, suppress_defaults=True)
    _add_project(p_search, suppress_defaults=True)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int)
    p_search.add_argument("--max-tokens", type=int)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    for name, help_text in (
        ("notes", "List indexed notes and index totals."),
        ("read", "Read a Markdown note in the vault."),
        ("recent", "List recent journal entries."),
        ("ranking", "Rank notes and queries by usage."),
    ):
        dashboard_parser = sub.add_parser(name, help=help_text)
        _add_common(dashboard_parser, suppress_defaults=True)
        _add_project(dashboard_parser, suppress_defaults=True)
        dashboard_parser.add_argument("--json", action="store_true", help="Emit the Bindery.app JSON contract.")
        if name == "read":
            dashboard_parser.add_argument("path")
        if name in {"recent", "ranking"}:
            dashboard_parser.add_argument("--limit", type=int)
        dashboard_parser.set_defaults(func=cmd_dashboard)

    p_status = sub.add_parser("status", help="Show index health. Exits non-zero on problems.")
    _add_common(p_status, suppress_defaults=True)
    _add_project(p_status, suppress_defaults=True)
    p_status.add_argument("--json", action="store_true", help="Emit the Bindery.app JSON contract.")
    p_status.set_defaults(func=cmd_status)

    p_clients = sub.add_parser("clients", help="Show detected and registered agent clients.")
    _add_common(p_clients, suppress_defaults=True)
    _add_project(p_clients, suppress_defaults=True)
    p_clients.add_argument("--json", action="store_true", help="Emit the Bindery.app JSON contract.")
    p_clients.set_defaults(func=cmd_clients)

    p_review = sub.add_parser("review", help="Show how the memory is growing and what needs attention.")
    _add_common(p_review, suppress_defaults=True)
    _add_project(p_review, suppress_defaults=True)
    p_review.add_argument("--min-count", type=int, default=2,
                          help="Minimum repeats before an unanswered query counts as a gap.")
    p_review.add_argument("--json", action="store_true")
    p_review.set_defaults(func=cmd_review)

    p_install = sub.add_parser("install", help="Print (or apply) configuration for every agent.")
    _add_common(p_install, suppress_defaults=True)
    _add_project(p_install, suppress_defaults=True)
    # Not a fixed `choices` list: the Codex account entries are discovered at
    # runtime, so the valid names depend on the machine.
    p_install.add_argument("client", nargs="?",
                           help="Limit output to one client. Default: all of them.")
    p_install.add_argument("--write", action="store_true",
                           help="Apply the configuration instead of printing it. Backs up first.")
    p_install.add_argument("--local", action="store_true",
                           help="Configure clients with project scope where supported "
                                "(Claude Code: ./.mcp.json; Antigravity: "
                                "./.agents/mcp_config.json). Codex remains global.")
    p_install.set_defaults(func=cmd_install)

    p_setup = sub.add_parser("setup", help="Index, configure every agent, and install the instructions.")
    _add_common(p_setup, suppress_defaults=True)
    _add_project(p_setup, suppress_defaults=True)
    p_setup.add_argument("--write", action="store_true",
                         help="Apply the changes. Without this, setup only reports what it would do.")
    p_setup.add_argument("--local", action="store_true",
                         help="Configure clients with project scope where supported "
                              "(Claude Code and Antigravity) instead of globally.")
    p_setup.set_defaults(func=cmd_setup)

    p_project = sub.add_parser(
        "project", help="Show or set which directories belong to which project."
    )
    _add_common(p_project, suppress_defaults=True)
    p_project.add_argument(
        "action", nargs="?", choices=["list", "which", "add", "remove"], default="list",
        help="list (default), which PATH, add NAME [PATH], remove NAME",
    )
    p_project.add_argument("name", nargs="?", help="Project name, for add/remove.")
    p_project.add_argument("path", nargs="?", help="Directory. Defaults to the current one.")
    p_project.add_argument(
        "--marker", action="store_true",
        help=f"Also write a {MARKER_HINT} file, so the name travels with the repository.",
    )
    p_project.set_defaults(func=cmd_project)

    p_prompt = sub.add_parser("prompt", help="Print the agent instructions that make the memory grow.")
    p_prompt.add_argument("--write", action="store_true",
                          help="Append to the agent instruction files instead of printing.")
    p_prompt.add_argument("--user", "--global", dest="user", action="store_true",
                          help="Write to the user-level agent instruction files, including "
                               "~/.gemini/GEMINI.md when ~/.gemini exists.")
    p_prompt.set_defaults(func=cmd_prompt)

    p_sync = sub.add_parser("sync", help="Synchronise a vault through an encrypted Git transport.")
    _add_common(p_sync, suppress_defaults=True)
    _add_project(p_sync, suppress_defaults=True)
    p_sync.add_argument("--json", action="store_true", help="Emit one JSON object and preserve the command exit code.")
    p_sync.add_argument("--allow-public-host", action="store_true", help="Allow an explicitly selected public Git host.")
    p_sync.add_argument("--key-store", choices=("auto", "keychain", "file"), default=argparse.SUPPRESS,
                         help="Store the sync key in auto-selected Keychain/file storage.")
    sync_sub = p_sync.add_subparsers(dest="sync_action")

    def _sync_common(parser):
        _add_common(parser, suppress_defaults=True)
        _add_project(parser, suppress_defaults=True)
        parser.add_argument("--key-store", choices=("auto", "keychain", "file"), default=argparse.SUPPRESS,
                            help="Store the sync key in auto-selected Keychain/file storage.")

    p_sync.set_defaults(func=cmd_sync, sync_action="run")
    p_sync_status = sync_sub.add_parser("status", help="Show encrypted sync state.")
    _sync_common(p_sync_status)
    p_sync_status.add_argument("--json", action="store_true")
    p_sync_status.set_defaults(func=cmd_sync, sync_action="status")

    p_sync_init = sync_sub.add_parser("init", help="Configure an encrypted remote.")
    _sync_common(p_sync_init)
    p_sync_init.add_argument("--remote", required=True)
    p_sync_init.add_argument("--write", action="store_true")
    p_sync_init.add_argument("--json", action="store_true")
    p_sync_init.add_argument("--allow-public-host", action="store_true")
    p_sync_init.set_defaults(func=cmd_sync, sync_action="init")

    p_sync_pair = sync_sub.add_parser("pair", help="Send the key over SSH without putting it in argv.")
    _sync_common(p_sync_pair)
    p_sync_pair.add_argument("host")
    p_sync_pair.add_argument("--replace", action="store_true")
    p_sync_pair.add_argument("--remote-vault", help="Absolute Vault path on the peer machine.")
    p_sync_pair.add_argument("--remote-bindery", help="Absolute bindery executable on the peer machine.")
    p_sync_pair.add_argument("--peer-remote",
                             help="The remote URL as the peer machine should spell it (default: this "
                                  "machine's). Use a local path when the peer hosts the bare repository.")
    p_sync_pair.add_argument("--remote-key-store", choices=("auto", "keychain", "file"), default="auto",
                             help="Key storage to request on the peer machine.")
    p_sync_pair.add_argument("--json", action="store_true")
    p_sync_pair.set_defaults(func=cmd_sync, sync_action="pair")

    p_accept = sync_sub.add_parser("accept-key", help="Receive a key on stdin from a trusted peer.")
    _sync_common(p_accept)
    p_accept.add_argument("--sync-id", required=True)
    p_accept.add_argument("--replace", action="store_true")
    p_accept.add_argument("--json", action="store_true")
    p_accept.set_defaults(func=cmd_sync, sync_action="accept-key")

    p_key = sync_sub.add_parser("key", help="Key recovery operations.")
    _sync_common(p_key)
    key_sub = p_key.add_subparsers(dest="key_action", required=True)
    p_export = key_sub.add_parser("export", help="Print a recovery string.")
    _sync_common(p_export)
    p_export.set_defaults(func=cmd_sync, sync_action="key-export")

    p_install_sync = sync_sub.add_parser("install", help="Install the opt-in scheduler.")
    _sync_common(p_install_sync)
    p_install_sync.add_argument("--interval", type=int, default=300)
    p_install_sync.add_argument("--write", action="store_true")
    p_install_sync.add_argument("--json", action="store_true")
    p_install_sync.set_defaults(func=cmd_sync, sync_action="install")

    p_uninstall_sync = sync_sub.add_parser("uninstall", help="Remove the scheduler.")
    _sync_common(p_uninstall_sync)
    p_uninstall_sync.add_argument("--write", action="store_true")
    p_uninstall_sync.add_argument("--json", action="store_true")
    p_uninstall_sync.set_defaults(func=cmd_sync, sync_action="uninstall")

    p_rekey = sync_sub.add_parser("rekey", help="Replace the encryption root (explicit and destructive).")
    _sync_common(p_rekey)
    p_rekey.add_argument("--write", action="store_true")
    p_rekey.add_argument("--purge-remote", action="store_true")
    p_rekey.add_argument("--json", action="store_true")
    p_rekey.set_defaults(func=cmd_sync, sync_action="rekey")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
