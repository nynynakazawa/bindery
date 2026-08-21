"""Codex transcripts.

One JSONL rollout file per session under ``<CODEX_HOME>/sessions/YYYY/MM/DD/``.
Several CODEX_HOMEs can exist on one machine - `codex-multi` gives each account
its own - and all of them are read, because a memory that covered only the
default account would miss exactly the background runs the split exists for.

The format wraps everything in ``{type, payload}``. What is worth keeping is
the user's message, the agent's visible reply, and what it ran; the reasoning
blocks, token counts, and injected developer instructions are not.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import Session, SessionRef, Turn

QUIET_SECONDS = 15 * 60

#: How much of one transcript to read. Sessions reach hundreds of megabytes -
#: pasted files, screenshots, build logs - and the reduced episode is capped at
#: a few thousand characters regardless, so reading further cannot change the
#: result. The cap is on bytes rather than lines because it is the bytes that
#: cost the time.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


class CodexAdapter:
    name = "codex"

    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else Path.home()

    def roots(self) -> list[Path]:
        roots = []
        default = self.home / ".codex" / "sessions"
        if default.is_dir():
            roots.append(default)
        accounts = self.home / ".codex-homes"
        if accounts.is_dir():
            for account in sorted(accounts.iterdir()):
                sessions = account / "sessions"
                if sessions.is_dir():
                    roots.append(sessions)
        return roots

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
                        # The rollout filename carries the session uuid, so the
                        # id is stable without opening the file.
                        session_id=path.stem.replace("rollout-", ""),
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
            payload = record.get("payload") or {}
            outer = record.get("type")
            inner = payload.get("type")

            if outer == "session_meta":
                ref.cwd = str(payload.get("cwd", "") or "")
            elif inner == "user_message":
                text = str(payload.get("message", "")).strip()
                if text and not text.startswith("<"):
                    session.turns.append(Turn("user", text))
            elif inner == "agent_message":
                text = str(payload.get("message", "")).strip()
                if text:
                    session.turns.append(Turn("agent", text))
            elif inner == "task_complete":
                text = str(payload.get("last_agent_message", "")).strip()
                if text:
                    session.turns.append(Turn("agent", text))
            elif inner == "function_call":
                _function_call(payload, session)
            elif inner == "function_call_output":
                session.turns.append(Turn("result", str(payload.get("output", ""))))
            # reasoning / token_count / developer messages are dropped: the
            # first is hidden thinking, the second is telemetry, the third is
            # the harness talking to itself.
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


def _function_call(payload: dict, session: Session) -> None:
    name = str(payload.get("name", ""))
    if name.startswith("bindery") or "memory_search" in name:
        return
    raw = payload.get("arguments")
    arguments: dict = {}
    if isinstance(raw, str):
        try:
            arguments = json.loads(raw)
        except ValueError:
            arguments = {}
    elif isinstance(raw, dict):
        arguments = raw

    command = arguments.get("cmd") or arguments.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if command:
        session.turns.append(Turn("command", str(command), name))
        return
    path = arguments.get("path") or arguments.get("file_path")
    if path:
        session.turns.append(Turn("edit", str(path), name))
    elif name:
        session.turns.append(Turn("command", name, name))
