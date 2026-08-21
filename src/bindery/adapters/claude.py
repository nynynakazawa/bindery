"""Claude Code transcripts.

One JSONL file per session under ``~/.claude/projects/<encoded-cwd>/``, one
JSON object per line. The lines worth keeping are ``user`` and ``assistant``;
everything else is application bookkeeping - queue operations, titles, hook
attachments, mode changes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import Session, SessionRef, Turn

#: How long a transcript must sit unmodified before it counts as finished.
#: There is no end-of-session marker in the format, so quiet is the only
#: available signal, and importing a session that is still being written would
#: capture half of it and never revisit the rest.
QUIET_SECONDS = 15 * 60

#: How much of one transcript to read. Sessions reach hundreds of megabytes -
#: pasted files, screenshots, build logs - and the reduced episode is capped at
#: a few thousand characters regardless, so reading further cannot change the
#: result. The cap is on bytes rather than lines because it is the bytes that
#: cost the time.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


class ClaudeAdapter:
    name = "claude"

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else Path.home()

    def roots(self) -> list[Path]:
        root = self.home / ".claude" / "projects"
        return [root] if root.is_dir() else []

    def discover(self) -> list[SessionRef]:
        refs: list[SessionRef] = []
        for root in self.roots():
            for path in sorted(root.rglob("*.jsonl")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                refs.append(
                    SessionRef(
                        path=path,
                        session_id=path.stem,
                        client=self.name,
                        modified=stat.st_mtime,
                    )
                )
        return refs

    def is_complete(self, ref: SessionRef) -> bool:
        return (time.time() - ref.modified) > QUIET_SECONDS

    def normalize(self, ref: SessionRef) -> Session:
        session = Session(ref=ref)
        for record in _lines(ref.path):
            kind = record.get("type")
            if not ref.cwd and record.get("cwd"):
                ref.cwd = str(record["cwd"])
            if kind == "user":
                _user(record, session)
            elif kind == "assistant":
                _assistant(record, session)
        session.project_hint = ref.cwd
        return session


def _lines(path: Path):
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    read = 0
    with handle:
        for line in handle:
            read += len(line)
            if read > MAX_TRANSCRIPT_BYTES:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _blocks(record: dict) -> list:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _user(record: dict, session: Session) -> None:
    if record.get("isSidechain"):
        # A subagent's own conversation. It has its own transcript, and
        # folding it in here would record the same work twice.
        return
    for block in _blocks(record):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            # Injected context, not something a person said.
            if text and not text.startswith(("<", "Caveat:")):
                session.turns.append(Turn("user", text))
        elif block.get("type") == "tool_result":
            session.turns.append(Turn("result", _flatten(block.get("content"))))


def _assistant(record: dict, session: Session) -> None:
    if record.get("isSidechain"):
        return
    for block in _blocks(record):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                session.turns.append(Turn("agent", text))
        elif block.get("type") == "tool_use":
            _tool_use(block, session)
        # "thinking" blocks are deliberately dropped: reasoning is not a
        # record of what happened, and it is the bulkiest thing in the file.


#: Tool inputs worth keeping, by tool name. The value is the field that says
#: what was actually done.
_WHAT_IT_DID = {
    "Bash": "command",
    "Edit": "file_path",
    "Write": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
}


def _tool_use(block: dict, session: Session) -> None:
    name = str(block.get("name", ""))
    if name.startswith("mcp__bindery__"):
        # Bindery's own traffic. Recording our searches as memory would make
        # the memory mostly a record of itself.
        return
    payload = block.get("input") or {}
    field = _WHAT_IT_DID.get(name)
    detail = str(payload.get(field, "")) if field else ""
    if name in {"Edit", "Write", "NotebookEdit"}:
        session.turns.append(Turn("edit", detail or name, name))
    elif name == "Bash":
        session.turns.append(Turn("command", detail, name))
    elif detail:
        session.turns.append(Turn("command", f"{name}: {detail}", name))


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""
