"""Reading what each agent wrote down about its own sessions.

Every agent keeps a transcript somewhere, in its own format, with its own idea
of what a session is. None of that is interesting to the rest of Bindery, so
it stops here: an adapter's whole job is to turn one vendor's log into the
same short list of turns, and the code above it never learns which agent it is
looking at.

That boundary is also what makes a third agent cheap. Adding one is a file in
this package; nothing in indexing, retrieval, or the tool surface changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class Turn:
    """One thing that happened, in a form worth keeping.

    Deliberately not a message. A tool call and its result are one turn, and a
    model's hidden reasoning is no turn at all.
    """

    kind: str                       # user | agent | command | edit | error | result
    text: str
    detail: str = ""


@dataclass(slots=True)
class SessionRef:
    """A transcript on disk, before anything has been read out of it."""

    path: Path
    session_id: str
    client: str
    started: float = 0.0
    modified: float = 0.0
    cwd: str = ""


@dataclass(slots=True)
class Session:
    """A transcript after normalisation."""

    ref: SessionRef
    turns: list[Turn] = field(default_factory=list)
    project_hint: str = ""


class Adapter(Protocol):
    """What every agent's transcript store has to be able to answer."""

    name: str

    def roots(self) -> list[Path]:
        """Directories this agent keeps transcripts in, if it is installed."""

    def discover(self) -> list[SessionRef]:
        """Every transcript this agent has on disk."""

    def is_complete(self, ref: SessionRef) -> bool:
        """Whether the session has ended and will not grow further."""

    def normalize(self, ref: SessionRef) -> Session:
        """Read one transcript into the shared shape."""


def available(home: Path | None = None) -> list[Adapter]:
    """Every adapter whose agent is present on this machine."""
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter

    adapters: list[Adapter] = [ClaudeAdapter(home), CodexAdapter(home)]
    return [adapter for adapter in adapters if adapter.roots()]
