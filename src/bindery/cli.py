"""Command line interface.

`serve` is what the agents run. The remaining commands exist so that a human
can inspect and repair the index without going through an agent, which matters
when the thing being debugged is the agent's own memory.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .indexer import reindex
from .search import search
from .store import Store


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", help="Directory of Markdown notes (default: $BINDERY_VAULT).")
    parser.add_argument("--state-dir", help="Where to keep the index.")
    parser.add_argument("--no-semantic", action="store_true", help="Keyword search only.")
    parser.add_argument(
        "--include",
        action="append",
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
        metavar="DIR",
        help="Never index this vault-relative directory. Repeatable.",
    )



def _add_project(parser: argparse.ArgumentParser) -> None:
    """Only for commands that read or write notes.

    `install` and `setup` already spell a different concept `--project` - which
    config file to write - so the flag stays off the shared block rather than
    meaning two things one subcommand apart.
    """
    parser.add_argument(
        "--project",
        help=(
            "Name the codebase these notes belong to (default: detected from the "
            "git remote of the working directory). Pass '' to disable scoping."
        ),
    )


def _config_from(args: argparse.Namespace) -> Config:
    return Config.resolve(
        vault=getattr(args, "vault", None),
        state_dir=getattr(args, "state_dir", None),
        max_tokens=getattr(args, "max_tokens", None),
        limit=getattr(args, "limit", None),
        semantic=False if getattr(args, "no_semantic", False) else None,
        include=getattr(args, "include", None),
        exclude=getattr(args, "exclude", None),
        # install/setup use --project as a flag meaning "write the config file
        # into this directory", so only accept the string form here.
        project=args.project if isinstance(getattr(args, "project", None), str) else None,
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
    print(json.dumps(report.as_dict(), indent=2))
    store.close()
    return 0


def _embed_missing(config: Config, store: Store) -> int:
    """Fill in vectors for passages that do not have one yet."""
    from .embed import load_backend

    backend = load_backend()
    if backend is None:
        print(
            "No embedding backend installed; keyword search remains active.\n"
            "Install one with:  uv tool install --force 'bindery-mcp[semantic]'",
            file=sys.stderr,
        )
        return 0
    rows = list(store.iter_chunks_without_vectors())
    if not rows:
        return 0
    batch_size = 32
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        texts = [f"{r['heading']}\n{r['body']}".strip() for r in batch]
        for row, vector in zip(batch, backend.encode(texts)):
            store.store_vector(int(row["id"]), vector)
            done += 1
        store.commit()
    return done


def cmd_search(args: argparse.Namespace) -> int:
    config = _config_from(args)
    store = Store(config.db_path)
    reindex(config, store)
    hits, meta = search(config, store, args.query, limit=args.limit, max_tokens=args.max_tokens)
    if args.json:
        print(json.dumps(
            {
                "meta": meta,
                "hits": [
                    {
                        "path": h.chunk.path,
                        "heading": h.chunk.heading,
                        "tokens": h.chunk.tokens,
                        "matched_by": h.matched_by,
                        "body": h.chunk.body,
                    }
                    for h in hits
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"{meta['returned']} passage(s), ~{meta['tokens']} tokens "
              f"({meta['truncated']} dropped by budget)\n")
        for hit in hits:
            where = hit.chunk.path + (f" # {hit.chunk.heading}" if hit.chunk.heading else "")
            print(f"--- {where}  [{hit.matched_by}]")
            print(hit.chunk.body)
            print()
    store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .embed import load_backend

    config = _config_from(args)
    store = Store(config.db_path)
    stats = store.stats()
    backend = load_backend() if config.semantic else None

    problems: list[str] = []
    if not config.vault.exists():
        problems.append(f"vault does not exist: {config.vault}")
    elif stats["notes"] == 0:
        problems.append(f"vault has no indexed Markdown notes: {config.vault}")
    if config.semantic and backend is None:
        problems.append("semantic search requested but no backend installed "
                        "(uv tool install --force 'bindery-mcp[semantic]') - keyword search still works")
    if backend is not None and stats["vectors"] < stats["chunks"]:
        problems.append(f"{stats['chunks'] - stats['vectors']} passage(s) not embedded yet "
                        "(run: bindery index --embed)")

    print(f"bindery {__version__}")
    print(f"  vault          {config.vault}")
    print(f"  index          {config.db_path}")
    print(f"  notes          {stats['notes']}")
    print(f"  passages       {stats['chunks']}")
    print(f"  links          {stats['links']}")
    print(f"  embedded       {stats['vectors']}")
    print(f"  max tokens     {config.max_tokens}")
    print(f"  semantic       {backend.name if backend else 'off (keyword only)'}")
    if problems:
        print("\nissues:")
        for problem in problems:
            print(f"  ! {problem}")
    else:
        print("\nready")
    store.close()
    return 1 if problems else 0


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
            lambda r: f"{r[1]:>4} retrievals  weight {r[2]:.2f}  {r[0]}")
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
  - you hit a dead end worth not repeating
  - a fix worked and the reason was not self-evident
  Always pass `tags`. Tags are what let a recurring topic graduate into a
  durable note of its own.
- **Do not** record routine progress, restatements of the code, or anything
  you could recover by reading the repository. Noise costs the same tokens as
  signal and crowds it out.
- Use `memory_write` for durable reference notes, `memory_learn` for the
  running record of what a session figured out.
- Both record the current project automatically. Pass `project=""` when what
  you learned is true regardless of which codebase you are in.
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


def _client_targets(config: Config, scope: str = "user") -> dict[str, dict]:
    """Config payloads for every supported client, keyed by client id.

    Scope matters and the two agents express it differently. Codex keeps MCP
    servers in one global ``config.toml``; Claude Code has both a per-project
    ``.mcp.json`` and a user-level entry in ``~/.claude.json``. Defaulting
    Claude Code to project scope would quietly make the memory work in one
    checkout and be missing everywhere else, while Codex saw it everywhere -
    so user scope is the default for both, and project scope is opt-in.
    """
    command, extra = _server_command()
    args = [*extra, "serve"]
    env = {"BINDERY_VAULT": str(config.vault)}
    claude_path = (
        Path.cwd() / ".mcp.json" if scope == "project" else Path.home() / ".claude.json"
    )
    return {
        "claude": {
            "label": "Claude Code",
            "path": claude_path,
            "scope": scope,
            "command": command,
            "args": args,
            "env": env,
        },
        "codex": {
            "label": "Codex",
            # Codex reads MCP servers from one global file; there is no
            # per-project equivalent to fall back to.
            "path": Path.home() / ".codex" / "config.toml",
            "scope": "user",
            "command": command,
            "args": args,
            "env": env,
        },
    }


def _detect_clients() -> set[str]:
    """Which agents look installed. Absence is a hint, never a blocker."""
    found: set[str] = set()
    if shutil.which("claude") or (Path.home() / ".claude").exists():
        found.add("claude")
    if shutil.which("codex") or (Path.home() / ".codex").exists():
        found.add("codex")
    return found


def _render_claude(spec: dict) -> str:
    payload = {
        "mcpServers": {
            "bindery": {
                "command": spec["command"],
                "args": spec["args"],
                "env": spec["env"],
            }
        }
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _render_codex(spec: dict) -> str:
    arg_list = ", ".join(f'"{a}"' for a in spec["args"])
    lines = [
        "[mcp_servers.bindery]",
        f'command = "{spec["command"]}"',
        f"args = [{arg_list}]",
        "",
        "[mcp_servers.bindery.env]",
    ]
    lines += [f'{key} = "{value}"' for key, value in spec["env"].items()]
    return "\n".join(lines)


def _write_claude(spec: dict) -> str:
    """Merge into Claude Code's JSON config, preserving everything else.

    ``~/.claude.json`` is not a config file the user wrote - it is live
    application state, tens of kilobytes of it. So this reads, adds one key
    under ``mcpServers``, and writes the whole document back unchanged
    otherwise, after taking a backup.
    """
    path: Path = spec["path"]
    existing: dict = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            existing = json.loads(raw)
        except ValueError:
            return f"! {path} is not valid JSON - left untouched."
        if not isinstance(existing, dict):
            return f"! {path} is not a JSON object - left untouched."
        path.with_suffix(path.suffix + ".bak").write_text(raw, encoding="utf-8")
    servers = existing.setdefault("mcpServers", {})
    servers["bindery"] = {
        "command": spec["command"],
        "args": spec["args"],
        "env": spec["env"],
    }
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"wrote {path} ({spec['scope']} scope)"


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


def _write_codex(spec: dict) -> str:
    """Write the server block, replacing an earlier one rather than duplicating it.

    Leaving an existing block untouched was the safer-looking choice and the
    wrong one: `bindery` moves when it is reinstalled elsewhere, and a stale
    ``command`` path means Codex silently loses the memory while the config
    still looks configured. The block is ours, keyed by our own name, so we
    replace it; anything else in the file is copied through untouched.
    """
    path: Path = spec["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current:
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(current, encoding="utf-8")
    remainder, replaced = _drop_codex_block(current)
    remainder = remainder.rstrip("\n")
    separator = "\n\n" if remainder else ""
    path.write_text(remainder + separator + _render_codex(spec) + "\n", encoding="utf-8")
    return f"updated {path}" if replaced else f"wrote {path}"


#: Where each agent reads standing instructions from.
#:
#: Project scope covers one repository; user scope covers every session on the
#: machine. A memory shared across projects wants user scope - installing per
#: project means repeating the step in every checkout and forgetting it in most
#: of them, and an agent that was never told to record anything records nothing.
PROMPT_TARGETS = {
    "project": [Path("AGENTS.md"), Path("CLAUDE.md")],
    "user": [Path(".codex") / "AGENTS.md", Path(".claude") / "CLAUDE.md"],
}


def cmd_prompt(args: argparse.Namespace) -> int:
    """Emit the agent instructions that make the memory actually accumulate."""
    if not args.write:
        print(AGENT_INSTRUCTIONS)
        return 0

    scope = "user" if args.user else "project"
    root = Path.home() if args.user else Path.cwd()
    written: list[str] = []
    for relative in PROMPT_TARGETS[scope]:
        target = root / relative
        if not target.parent.exists():
            print(f"- {target}: parent directory missing - skipped.")
            continue
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if "Shared memory (Bindery)" in current:
            print(f"- {target}: already has the block - left untouched.")
            continue
        if current:
            # These files often hold a lot of hand-written policy. Never edit
            # one without leaving the previous version recoverable.
            target.with_suffix(target.suffix + ".bak").write_text(current, encoding="utf-8")
        prefix = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
        target.write_text(current + prefix + AGENT_INSTRUCTIONS, encoding="utf-8")
        written.append(str(target))

    if written:
        print("appended to:")
        for path in written:
            print(f"  {path}")
    else:
        print("nothing written.")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Print - or apply - client configuration.

    Both agents are first-class here. With no client named, every supported
    client is emitted, so nothing about the setup implies one of them is the
    primary and the other an afterthought.
    """
    config = _config_from(args)
    scope = "project" if getattr(args, "project", False) else "user"
    targets = _client_targets(config, scope)
    chosen = [args.client] if args.client else sorted(targets)
    detected = _detect_clients()

    for index, client in enumerate(chosen):
        spec = targets[client]
        if index:
            print()
        seen = " (detected)" if client in detected else " (not detected on this machine)"
        print(f"# {spec['label']}{seen} - {spec['scope']} scope - {spec['path']}")
        print()
        print(_render_claude(spec) if client == "claude" else _render_codex(spec))
        if args.write:
            print()
            print(_write_claude(spec) if client == "claude" else _write_codex(spec))

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
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Do every setup step in one pass.

    Defaults to a dry run. The prompt step edits the agent policy files in your
    home directory, which are usually hand-maintained and long, so showing what
    will be touched before touching it is the right default.
    """
    config = _config_from(args)
    mode = "APPLYING" if args.write else "DRY RUN - nothing will be written"
    print(f"bindery setup ({mode})")
    print(f"  vault: {config.vault}")

    print("\n[1/3] index")
    if args.write:
        store = Store(config.db_path)
        report = reindex(config, store)
        store.close()
        print(f"  {report.scanned} note(s) scanned, {report.added} added, {report.updated} updated")
    else:
        print(f"  would index {config.vault}")

    print("\n[2/3] MCP server configuration")
    targets = _client_targets(config, "project" if args.project else "user")
    detected = _detect_clients()
    for client in sorted(targets):
        spec = targets[client]
        mark = "detected" if client in detected else "not detected"
        if args.write:
            result = _write_claude(spec) if client == "claude" else _write_codex(spec)
            print(f"  {spec['label']} ({mark}): {result}")
        else:
            print(f"  {spec['label']} ({mark}): would write {spec['path']} [{spec['scope']} scope]")

    print("\n[3/3] agent instructions (this is the step that makes anything get recorded)")
    if args.write:
        prompt_args = argparse.Namespace(write=True, user=True)
        cmd_prompt(prompt_args)
    else:
        for relative in PROMPT_TARGETS["user"]:
            target = Path.home() / relative
            state = "exists" if target.exists() else "would be created"
            print(f"  would append to {target} ({state})")

    if not args.write:
        print("\nRe-run with --write to apply. Existing files are backed up, never replaced.")
    else:
        print("\nDone. Restart your agents so they pick up the new configuration.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bindery",
        description="One shared memory for Claude Code and Codex.",
    )
    parser.add_argument("--version", action="version", version=f"bindery {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the MCP server on stdio (what agents launch).")
    _add_common(p_serve)
    _add_project(p_serve)
    p_serve.add_argument("--max-tokens", type=int, help="Default response budget.")
    p_serve.add_argument("--limit", type=int, help="Default passages per search.")
    p_serve.set_defaults(func=cmd_serve)

    p_index = sub.add_parser("index", help="Scan the vault and refresh the index.")
    _add_common(p_index)
    _add_project(p_index)
    p_index.add_argument("--force", action="store_true", help="Reindex every note.")
    p_index.add_argument("--embed", action="store_true", help="Also compute missing embeddings.")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="Search from the terminal.")
    _add_common(p_search)
    _add_project(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int)
    p_search.add_argument("--max-tokens", type=int)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_status = sub.add_parser("status", help="Show index health. Exits non-zero on problems.")
    _add_common(p_status)
    _add_project(p_status)
    p_status.set_defaults(func=cmd_status)

    p_review = sub.add_parser("review", help="Show how the memory is growing and what needs attention.")
    _add_common(p_review)
    _add_project(p_review)
    p_review.add_argument("--min-count", type=int, default=2,
                          help="Minimum repeats before an unanswered query counts as a gap.")
    p_review.add_argument("--json", action="store_true")
    p_review.set_defaults(func=cmd_review)

    p_install = sub.add_parser("install", help="Print (or apply) configuration for every agent.")
    _add_common(p_install)
    p_install.add_argument("client", nargs="?", choices=["claude", "codex"],
                           help="Limit output to one client. Default: all of them.")
    p_install.add_argument("--write", action="store_true",
                           help="Apply the configuration instead of printing it. Backs up first.")
    p_install.add_argument("--project", action="store_true",
                           help="Configure Claude Code for this project only (.mcp.json) instead "
                                "of every project. Codex has no project scope and is unaffected.")
    p_install.set_defaults(func=cmd_install)

    p_setup = sub.add_parser("setup", help="Index, configure every agent, and install the instructions.")
    _add_common(p_setup)
    p_setup.add_argument("--write", action="store_true",
                         help="Apply the changes. Without this, setup only reports what it would do.")
    p_setup.add_argument("--project", action="store_true",
                         help="Configure Claude Code for this project only instead of globally.")
    p_setup.set_defaults(func=cmd_setup)

    p_prompt = sub.add_parser("prompt", help="Print the agent instructions that make the memory grow.")
    p_prompt.add_argument("--write", action="store_true",
                          help="Append to the agent instruction files instead of printing.")
    p_prompt.add_argument("--user", "--global", dest="user", action="store_true",
                          help="Write to ~/.codex/AGENTS.md and ~/.claude/CLAUDE.md so the "
                               "instructions apply in every project, not just this one.")
    p_prompt.set_defaults(func=cmd_prompt)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
