"""Turning past sessions into memory nobody had to remember to write.

`memory_learn` only records what an agent thought to record. Everything an
agent learned and did not write down used to be lost the moment the session
ended - and the sessions where that matters most are the ones that went badly,
because those end in frustration rather than in tidy conclusions.

So the transcripts themselves become searchable. Not summarised: reduced.
Nothing here calls a model, which is the point - a summary is a second thing
that can be wrong, it costs an API call per session, and it quietly discards
the detail that turns out to matter. What this does instead is throw away the
parts of a transcript that were never knowledge in the first place, keep the
rest verbatim, and let retrieval decide what is worth surfacing.

Episodes are the lowest tier of memory (see ``indexer.TIER_PRIOR``). A note
someone wrote on purpose outranks a transcript of them working it out, always.
An episode's job is to be there when nothing better is.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .adapters import Session, SessionRef, available
from .config import Config
from .indexer import EPISODE_PREFIX, index_path
from .redact import redact
from .safeio import atomic_write, file_lock
from .store import Store
from .tokens import estimate_tokens

#: Where the record of what has already been imported lives.
STATE_FILE = "episodes.json"

#: Per-turn caps. A pasted file or a thousand-line build log is not a memory
#: of anything; the first few hundred characters say what it was.
MAX_USER_CHARS = 1500
MAX_AGENT_CHARS = 1200
MAX_COMMAND_CHARS = 300
MAX_ERROR_CHARS = 800

#: Whole-episode cap. Past this the session was long rather than eventful, and
#: the remainder is almost always more of the same.
MAX_EPISODE_CHARS = 16000

#: A session needs at least this many substantive turns to be worth a file.
MIN_TURNS = 3

#: And at least one request with some substance to it. Measured in estimated
#: tokens rather than characters: forty characters is a terse English sentence
#: and a long Japanese one, so a character threshold silently applies a
#: different standard to each language.
MIN_REQUEST_TOKENS = 12

#: Output that is worth keeping even though it is output: something failed, or
#: something reported a result.
_INTERESTING_OUTPUT = re.compile(
    r"(?im)^.*(?:"
    r"error|exception|traceback|fatal|failed|failure|assertion"
    r"|\berror\b|\bE\s{3}"
    r"|\d+\s+(?:passed|failed|error)"
    r"|exited with code [1-9]"
    r"|command not found|no such file|permission denied"
    r"|cannot find|undefined|unresolved"
    r").*$"
)

#: Paths whose contents are never anyone's knowledge.
_NOISE_PATH = re.compile(r"(?:node_modules|\.venv|site-packages|dist/|build/|\.git/)")

#: Runs of whitespace that a transcript accumulates and a note does not need.
_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass(slots=True)
class ImportReport:
    scanned: int = 0
    imported: int = 0
    skipped: int = 0
    incomplete: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "imported": self.imported,
            "skipped": self.skipped,
            "incomplete": self.incomplete,
            "failed": self.failed,
        }


# ------------------------------------------------------------------- state


def _state_path(config: Config) -> Path:
    return config.state_dir / STATE_FILE


def load_state(config: Config) -> dict:
    path = _state_path(config)
    if not path.exists():
        return {"baseline": [], "imported": [], "quarantined": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"baseline": [], "imported": [], "quarantined": []}
    for key in ("baseline", "imported", "quarantined"):
        data.setdefault(key, [])
    return data


def save_state(config: Config, state: dict) -> None:
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def set_baseline(config: Config, home: Path | None = None) -> int:
    """Mark every session that already exists as not to be imported.

    Installing a memory layer must not mean silently ingesting years of past
    conversations - a decision the user did not make, about data they may have
    forgotten is there. Capture starts from now.
    """
    state = load_state(config)
    existing = {
        f"{ref.client}:{ref.session_id}"
        for adapter in available(home)
        for ref in adapter.discover()
    }
    state["baseline"] = sorted(existing | set(state.get("baseline", [])))
    save_state(config, state)
    return len(existing)


# ----------------------------------------------------------------- reducing


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[… {len(text) - limit} more characters]"


def _keep_output(text: str) -> str:
    """Almost all command output is discarded. Failures are not."""
    if not text or _NOISE_PATH.search(text):
        return ""
    lines = [m.group(0).strip() for m in _INTERESTING_OUTPUT.finditer(text)]
    if not lines:
        return ""
    seen, unique = set(), []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            unique.append(line)
    return _clip("\n".join(unique[:12]), MAX_ERROR_CHARS)


def reduce_session(session: Session) -> str:
    """Render a normalised session as the Markdown that will be indexed.

    Consecutive identical commands collapse, output survives only when it
    reports a failure, and everything is redacted. What is left is what a
    person would have written down if they had been taking notes.
    """
    parts: list[str] = []
    previous_command = ""
    pending_commands: list[str] = []

    def flush_commands() -> None:
        nonlocal pending_commands
        if not pending_commands:
            return
        block = "\n".join(pending_commands)
        parts.append(f"```\n{block}\n```")
        pending_commands = []

    for turn in session.turns:
        if turn.kind == "user":
            flush_commands()
            parts.append(f"**User:** {_clip(turn.text, MAX_USER_CHARS)}")
        elif turn.kind == "agent":
            flush_commands()
            parts.append(_clip(turn.text, MAX_AGENT_CHARS))
        elif turn.kind in {"command", "edit"}:
            text = _clip(turn.text, MAX_COMMAND_CHARS)
            if not text or text == previous_command:
                continue
            previous_command = text
            prefix = "edited" if turn.kind == "edit" else "$"
            pending_commands.append(f"{prefix} {text}")
        elif turn.kind == "result":
            kept = _keep_output(turn.text)
            if kept:
                flush_commands()
                parts.append(f"```\n{kept}\n```")

    flush_commands()
    body = _BLANK_RUN.sub("\n\n", "\n\n".join(parts).strip())
    if len(body) > MAX_EPISODE_CHARS:
        body = body[:MAX_EPISODE_CHARS].rstrip() + "\n\n[… session continues]"
    return redact(body)


def worth_recording(session: Session) -> bool:
    """Skip the sessions that were not sessions.

    A one-question lookup teaches nothing that the answer to it does not
    already record, and writing a file per such exchange would bury the ones
    that matter.
    """
    substantive = [t for t in session.turns if t.kind in {"user", "agent"}]
    if len(substantive) < MIN_TURNS:
        return False
    return any(
        t.kind == "user" and estimate_tokens(t.text) >= MIN_REQUEST_TOKENS
        for t in substantive
    )


# ---------------------------------------------------------------- importing


def _episode_path(config: Config, ref: SessionRef, project: str) -> str:
    day = datetime.datetime.fromtimestamp(ref.modified or 0).date().isoformat()
    short = ref.session_id.replace("-", "")[:8] or "unknown"
    folder = project or "_global"
    return f"{EPISODE_PREFIX}{folder}/{day}-{ref.client}-{short}.md"


def _title(session: Session) -> str:
    for turn in session.turns:
        if turn.kind == "user" and turn.text.strip():
            first = turn.text.strip().splitlines()[0]
            return _clip(first, 80).replace("\n", " ")
    return f"{session.ref.client} session"


def import_new(
    config: Config,
    store: Store,
    *,
    home: Path | None = None,
    limit: int | None = None,
) -> ImportReport:
    """Capture every finished session that has not been captured before.

    Called at server startup rather than from a daemon or an editor hook.
    That is a deliberate simplification: whichever agent starts next imports
    whatever the others left behind, so a Claude session that ended overnight
    is searchable from Codex in the morning with nothing running in between.
    """
    report = ImportReport()
    state = load_state(config)
    known = set(state["baseline"]) | set(state["imported"]) | set(state["quarantined"])
    imported: list[str] = []

    for adapter in available(home):
        for ref in adapter.discover():
            key = f"{ref.client}:{ref.session_id}"
            if key in known:
                continue
            report.scanned += 1
            if not adapter.is_complete(ref):
                report.incomplete += 1
                continue
            if limit is not None and report.imported >= limit:
                break
            try:
                session = adapter.normalize(ref)
            except Exception:
                # A malformed transcript is quarantined rather than retried
                # forever: it will not become readable, and one bad file must
                # not stall every import after it.
                state["quarantined"].append(key)
                report.failed += 1
                continue
            if not worth_recording(session):
                imported.append(key)
                report.skipped += 1
                continue
            try:
                _write_episode(config, store, session)
            except Exception:
                report.failed += 1
                continue
            imported.append(key)
            report.imported += 1

    if imported or report.failed:
        state["imported"] = sorted(set(state["imported"]) | set(imported))
        save_state(config, state)
    return report


def _write_episode(config: Config, store: Store, session: Session) -> str:
    from .workspace import resolve

    hint = Path(session.project_hint) if session.project_hint else None
    project = resolve(hint, state_dir=config.state_dir).name if hint else ""
    rel = _episode_path(config, session.ref, project)
    target = config.vault / rel

    body = reduce_session(session)
    front = [
        "---",
        f"title: {_title(session)}",
        f"project: {project}",
        f"client: {session.ref.client}",
        f"session: {session.ref.session_id}",
        "---",
        "",
    ]
    text = "\n".join(front) + body.lstrip("\n") + "\n"

    with file_lock(target, config.state_dir):
        atomic_write(target, text)
    index_path(config, store, target)
    return rel
