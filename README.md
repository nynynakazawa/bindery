# Bindery

**One shared memory for Claude Code and Codex.**

[日本語版 README](README.ja.md)

Bindery is an [MCP](https://modelcontextprotocol.io) server that gives your
coding agents a single, persistent knowledge base that **gets better as you use
it**. Claude Code writes a decision down; Codex finds it in the next session.
Retrieval learns from itself, so the notes that keep answering questions rise and
the ones that stopped mattering fade. Your notes stay as plain Markdown on disk,
so Obsidian, `grep`, and `git` all keep working.

---

## Why this exists

Three problems, in the order people usually hit them.

**Every session starts from zero.** You re-explain the architecture, the
decisions you already made, and the two approaches you already rejected. That
re-explanation is pure token cost, paid again on every new terminal.

**Pointing an agent at a note folder does not scale.** Once a vault is large,
loading it is slow, and loading *part* of it means guessing which part. Past a
certain size the agent either runs out of context or reads the wrong files.

**Two agents means two memories.** Claude Code and Codex each have their own
native memory (`CLAUDE.md`, `AGENTS.md`, Codex Memories). None of it crosses
over. Work done in one is invisible to the other.

**A memory that only stores is a filing cabinet.** Notes pile up, nothing is ever
consolidated, and nobody can tell which ones are earning their keep or what the
collection is missing.

Bindery answers all three with one idea: **keep the Markdown, replace the
loading.** Notes stay files. Retrieval becomes a search that returns matching
passages under a hard token budget, from an index both agents share.

---

## What it does

- **Shared across agents.** Both clients point at the same vault and therefore
  the same index. A note written by one is searchable by the other immediately.
- **Passage retrieval, not file loading.** Notes are split at Markdown headings.
  A match returns that section, not the whole document.
- **A hard token budget.** Every search response is capped (2,000 tokens by
  default). When matches do not fit, they are dropped and the response says so,
  so you can tell "nothing matched" from "too much matched".
- **Japanese and English out of the box.** Full-text search uses SQLite FTS5
  with the `trigram` tokenizer, which indexes CJK without a morphological
  analyser. Two-character queries (`認証`, `設計`) fall back to a substring scan,
  because trigram cannot represent them.
- **Obsidian-compatible.** `[[wiki links]]` become a navigable graph. The vault
  is only ever read and written as Markdown files.
- **It grows on its own.** Every search is a training signal. Notes that answer
  questions get ranked higher; notes that stopped being useful decay back down;
  questions that returned nothing are logged as knowledge gaps in the agents' own
  words. No model calls, no configuration, no curation required.
- **Journal, then graduate.** `memory_learn` appends what an agent learned to a
  daily journal. When a topic recurs across enough days, it is surfaced as ready
  to become a durable note of its own.
- **Automatic session records.** When a client disconnects, the server writes
  down what it observed by itself - the questions that came back empty, the
  notes that were touched. No model call, no agent cooperation required.
- **Consolidation, so growth does not become sprawl.** Near-duplicate passages and
  never-retrieved notes are detected automatically and reported by
  `memory_review`.
- **Optional semantic search.** Install one extra and hybrid keyword + vector
  retrieval turns on. It is optional because keyword search alone already works.
- **One dependency: the official MCP SDK.** Everything else is the Python
  standard library. The protocol layer was hand-written for a while to keep
  even that off the list, but the wire format is the fastest-moving part of MCP
  and the least specific to this project - version negotiation alone is worth
  more than the dependency costs.

## What it deliberately does not do

Cut on purpose, to keep the tool surface and the install small:

| Omitted | Why |
| --- | --- |
| Cloud sync, team workspaces | Local-first. Your notes are your files. |
| Accounts, auth, multi-tenancy | Single-user tool. |
| LLM-based memory extraction | It would call a paid API on every write - the opposite of the goal. |
| Whole-codebase AST indexing | A different problem. Use a code-intelligence server alongside this. |
| Dashboards, web UI | Your editor is the UI. |
| Automatic merging or deletion of notes | Duplicates and stale notes are *detected* automatically, but acting on them needs judgment. Silently rewriting your notes is how a memory layer destroys what it was meant to protect. |
| Reasoning traces, formation metrics | Unproven value for the token cost they add. |

The tool surface is **eight tools**. That number is a design decision, not an
accident: tool schemas are sent to the model on every session, so every tool has
a standing token cost whether or not it is ever called. A test asserts the count,
so raising it is a deliberate act.

---

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
git clone https://github.com/nynynakazawa/bindery.git && cd bindery
uv tool install .
```

That puts `bindery` on your PATH in its own isolated environment. `pipx install .`
does the same if you already use pipx.

Prefer these over `pip install -e .`. A bare `pip install` fails outright on a
Homebrew or system Python ([PEP 668](https://peps.python.org/pep-0668/)), and
inside a project `.venv` the `bindery` command exists only while that venv is
activated - which it is not when an agent launches the server. Use `pip` only
inside a virtualenv you manage yourself, and expect to point your agents at the
full path.

Optional semantic search (adds a local embedding model, roughly 100 MB, no API
calls and no per-query cost):

```bash
uv tool install '.[semantic]'
bindery index --embed
```

## Set up

**Use `setup --write`.** It is the recommended path and the only one that
completes every step - indexing the vault, configuring every agent it finds, and
installing the agent instructions without which nothing is ever recorded.

```bash
export BINDERY_VAULT="$HOME/Obsidian/MyVault"   # an existing Obsidian vault works as-is

bindery setup            # preview: lists every file it would touch
bindery setup --write    # do it
bindery status           # confirm
```

That is the whole installation.

`setup` previews by default rather than acting, because its last step appends to
the agent policy files in your home directory - long, hand-maintained files worth
looking at before something edits them. Existing files are backed up, never
replaced, and re-running changes nothing.

<details>
<summary>Doing it by hand instead</summary>

The individual commands below are what `setup --write` runs for you. Reach for
them when you want one piece on its own - a different scope, one agent only, or
the instruction block pasted somewhere of your choosing. Otherwise skip to
[Tools](#tools).

</details>

## Connect your agents

Neither agent is the primary one. `install` with no argument emits the
configuration for every supported client and tells you which it can see:

```bash
bindery install              # print config for all clients
bindery install --write      # apply it (backs up first, never clobbers
                                  # servers you already configured)
bindery install codex        # or just one
```

For reference, the two forms it produces:

```jsonc
// Claude Code - .mcp.json
{ "mcpServers": { "bindery": {
    "command": "bindery", "args": ["serve"],
    "env": { "BINDERY_VAULT": "/Users/you/Obsidian/MyVault" } } } }
```

```toml
# Codex - ~/.codex/config.toml
[mcp_servers.bindery]
command = "bindery"
args = ["serve"]

[mcp_servers.bindery.env]
BINDERY_VAULT = "/Users/you/Obsidian/MyVault"
```

**Point every client at the same `BINDERY_VAULT`.** That single shared value
is what makes the memory shared - the index location is derived from it.

## Teach the agents to use it

This step is not optional, and it is the one people skip.

An MCP server can only answer calls. It cannot make an agent record anything,
so nothing is written unless the agent decides to write it. These instructions
are that decision:

```bash
bindery prompt --global --write   # ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md
bindery prompt --write            # or just this project's CLAUDE.md / AGENTS.md
bindery prompt                    # or print, and paste where you like
```

Prefer `--global`. A vault shared across projects needs instructions that are
shared across projects too; installing per project means repeating the step in
every checkout and forgetting it in most of them, and an agent that was never
told to record anything records nothing.

The block is client-neutral and identical in both files.

Note that `install` does **not** do this step. Configuring the server and
teaching the agents to use it are separate things, and only the second one
determines whether anything is ever written down.

---

## Tools

| Tool | Purpose |
| --- | --- |
| `memory_search` | Search and return matching passages under a token budget. |
| `memory_read` | Read one note in full, truncated at the budget. |
| `memory_write` | Create or overwrite a note and index it immediately. |
| `memory_learn` | Record what this session learned into today's journal. |
| `memory_review` | Gaps, load-bearing notes, duplicates, stale notes, promotion candidates. |
| `memory_links` | Outgoing and incoming `[[wiki links]]` for a note. |
| `memory_status` | Vault path, index size, semantic search state. |
| `memory_reindex` | Rescan the vault after external edits. |

A prompt that works well in `CLAUDE.md` and `AGENTS.md`:

> Before starting work, call `memory_search` for relevant prior decisions.
> Whenever you learn something the next session would otherwise have to
> rediscover - a decision, a constraint, a dead end, a fix that worked - record
> it with `memory_learn` and tag the topic.
> Use `memory_write` for durable reference notes.

`memory_learn` is what closes the loop. Searching alone teaches the ranking what
matters, but only writing adds knowledge - and **`memory_learn` is never called
automatically**. No threshold triggers it; MCP servers cannot initiate anything.
The instructions installed by `bindery prompt` are the entire mechanism,
which is why that step matters more than it looks.

What *is* automatic is the session record described below - but it captures
activity, not insight.

## CLI

```bash
bindery serve                    # MCP server on stdio (what agents launch)
bindery index [--force] [--embed]
bindery search "認証方式" [--limit N] [--max-tokens N] [--json]
bindery status                   # exits non-zero when something is wrong
bindery setup [--write]          # everything below, in one pass
bindery review [--json] [--min-count N]
bindery install [claude|codex] [--write]
bindery prompt [--write] [--global]
```

`review` is the one to run every few weeks. It answers: what did the agents keep
asking that I never wrote down, which notes are doing the work, and what has
turned into clutter.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BINDERY_VAULT` | `./memory` | Directory of Markdown notes. |
| `BINDERY_STATE_DIR` | `~/.bindery` | Where the index lives. |
| `BINDERY_MAX_TOKENS` | `2000` | Default search response cap. |
| `BINDERY_LIMIT` | `8` | Default passages per search. |
| `BINDERY_CHUNK_TOKENS` | `400` | Target passage size. |
| `BINDERY_SEMANTIC` | on | Set to `0` to force keyword-only. |
| `BINDERY_AUTOCAPTURE` | on | Set to `0` to stop writing session records. |

Growth tuning lives in `growth.py` as named constants - `USAGE_HALF_LIFE_DAYS`,
`USAGE_BOOST_WEIGHT`, `DUPLICATE_THRESHOLD`, `STALE_AFTER_DAYS`,
`PROMOTION_MIN_ENTRIES`. They are deliberately not environment variables: they
change retrieval behaviour and should be changed with a test run, not a shell
export.

---

## How it works

```
Obsidian / your editor          Claude Code            Codex
        |                            |                   |
        | edits .md                  |  MCP stdio        |  MCP stdio
        v                            v                   v
   ┌─────────────┐            ┌──────────────────────────────┐
   │   Vault     │  scan  ->  │   Bindery MCP server    │
   │  *.md files │            │  chunk / index / retrieve    │
   └─────────────┘            └──────────────┬───────────────┘
   source of truth                           │
                                             v
                                 ┌───────────────────────┐
                                 │ SQLite (WAL)          │
                                 │  FTS5 trigram + BM25  │
                                 │  vectors (optional)   │
                                 └───────────────────────┘
```

**Markdown is the source of truth.** The database is a derived index. Delete it
and it rebuilds; your notes are never trapped inside it.

**Chunking is where the savings come from.** A retrieval system that returns
whole notes will hand an agent a 4,000-token document because one sentence
matched. Returning the matching section instead is the difference between a
memory layer that pays for itself and one that costs more than it saves.

**Two rankings, fused.** BM25 over the trigram index, and - when embeddings are
installed - cosine similarity over chunk vectors. They are combined with
Reciprocal Rank Fusion, which needs no score calibration between two metrics
living on different scales.

**Incremental by content hash.** A file that is touched but not edited costs
nothing to rescan. The server also rescans on startup, so edits made in Obsidian
while no agent was running are picked up automatically.

**Concurrency.** SQLite in WAL mode with a busy timeout, so both agents can hold
the index open at once.

### The growth loop

```
   search ──> what was asked, and what answered it ──┐
      ^                                              │
      │                                              v
   ranking <── usage weight (frequency x recency) <── telemetry
      ^                                              │
      │                                              v
   retrieval quality                        review: gaps, duplicates,
                                            stale notes, promotions
```

**Usage is the training signal, and it is free.** Each search records the query
and the notes that answered it. A note's weight is `log(frequency)` times an
exponential recency decay with a 30-day half-life, so heavy use cannot let one
note dominate and knowledge that stopped being useful quietly stops being
boosted. The boost is capped at 25%: history may break ties, but it must never
outrank an actual content match.

**Usage is keyed by note path, not chunk id.** Chunk ids are rebuilt on every
reindex; keying on them would erase everything the system had learned each time
a file was edited.

**Misses are recorded too.** A query that returns nothing is the clearest
statement the system ever gets about what it is missing, phrased by the agent
that wanted it. Repeats of the same miss become `knowledge_gaps`.

**Automatic capture has a threshold, and it is deliberately strict.** When a
client disconnects, the server writes a record to `journal/sessions/` only if
the session produced at least `AUTO_CAPTURE_MIN_SIGNALS` (2) *signals*. A signal
is an unanswered question, a note written, or a learning recorded - never a
successful search, because a session that found everything it needed taught the
system nothing. Sessions below the threshold write nothing at all, which is what
keeps routine lookups from filling the vault.

**Session records are written but never indexed.** This looks like an
inconsistency and is the opposite. Those records list the questions a session
could *not* answer. Index them and the record of a missing answer starts
matching the very query it was written about, so the second time anyone asks,
the gap looks answered and quietly disappears from `memory_review` - broken
precisely because it had been detected once. A note saying "nobody knew X" must
never be retrievable as an answer about X. Gaps live in the queries table, which
nothing written into the vault can contaminate.

**Consolidation stops at detection.** Near-duplicates are found with bottom-k
shingle sketches and scored by *containment* rather than Jaccard, because the
redundancy that matters usually looks like one passage being another plus a
sentence - an asymmetry Jaccard punishes and containment reports correctly.
Candidate pairs come from an inverted index over sketch values, which keeps the
comparison well clear of quadratic.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).

Bindery is a clean-room implementation. [NOTICE](NOTICE) records the
projects whose designs informed it - Memorix, codebase-memory-mcp, mem0,
Basic Memory, and ByteRover/Cipher - and states explicitly that no code from any
of them is included. In particular, no code was taken from Basic Memory
(AGPL-3.0) or ByteRover/Cipher (Elastic License 2.0), so this project is neither
a derivative work of the former nor subject to the latter's restrictions.
