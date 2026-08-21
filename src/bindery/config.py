"""Configuration resolution.

Every setting has a working default so that `python3 -m bindery serve`
does something sensible with no arguments. Environment variables override the
defaults; explicit CLI flags override the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Directory that holds the SQLite index. Kept out of the vault so that the
#: vault stays a clean set of Markdown files that Obsidian and git can own.
DEFAULT_STATE_DIR = Path.home() / ".bindery"

#: Response budgets. `max_tokens` is the hard cap applied to a single search
#: response - the single most important knob in this project, because an
#: unbounded response is exactly what makes a large note collection unusable.
DEFAULT_MAX_TOKENS = 2000
DEFAULT_LIMIT = 8

#: Chunking. Sections larger than this are split so that retrieval returns a
#: passage rather than an entire long note.
DEFAULT_CHUNK_TOKENS = 400
DEFAULT_CHUNK_OVERLAP = 40


def _env_list(name: str) -> list[str]:
    """Comma-separated vault-relative prefixes from the environment."""
    raw = os.environ.get(name, "")
    return _clean_prefixes(raw.split(","))


def _clean_prefixes(values) -> list[str]:
    """Normalise path prefixes so comparisons are not defeated by punctuation."""
    cleaned = []
    for value in values:
        text = str(value).strip().strip("/").replace("\\", "/")
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def detect_project(start: Path | None = None, state_dir: Path | None = None) -> str:
    """Name the codebase the agent is working in, or "" if there is no telling.

    See :mod:`bindery.workspace` for how this is decided and why it is not
    simply the directory name.
    """
    from .workspace import resolve

    return resolve(start, state_dir=state_dir).name


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


@dataclass
class Config:
    """Resolved runtime configuration."""

    vault: Path
    state_dir: Path
    max_tokens: int = DEFAULT_MAX_TOKENS
    limit: int = DEFAULT_LIMIT
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    semantic: bool = True
    autocapture: bool = True
    #: Import finished sessions from the other agents' transcripts.
    episodes: bool = True
    #: Vault-relative prefixes that may be indexed. Empty means "all of it".
    include: list[str] = field(default_factory=list)
    #: Vault-relative prefixes that must never be indexed.
    exclude: list[str] = field(default_factory=list)
    #: Which codebase this server is answering for. "" disables scoping.
    project: str = ""

    @property
    def db_path(self) -> Path:
        """One index per vault, keyed by the vault's absolute path.

        Two agents pointed at the same vault therefore land on the same
        database file, which is what makes the memory genuinely shared rather
        than merely duplicated.
        """
        key = _slugify(str(self.vault.resolve()))
        return self.state_dir / f"index-{key}.db"

    @classmethod
    def resolve(
        cls,
        vault: Path | str | None = None,
        *,
        state_dir: Path | str | None = None,
        max_tokens: int | None = None,
        limit: int | None = None,
        semantic: bool | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        project: str | None = None,
    ) -> "Config":
        resolved_vault = (
            Path(vault).expanduser()
            if vault
            else _env_path("BINDERY_VAULT") or (Path.cwd() / "memory")
        )
        resolved_state = (
            Path(state_dir).expanduser()
            if state_dir
            else _env_path("BINDERY_STATE_DIR") or DEFAULT_STATE_DIR
        )
        env_capture = os.environ.get("BINDERY_AUTOCAPTURE", "").strip().lower()
        env_episodes = os.environ.get("BINDERY_EPISODES", "").strip().lower()
        env_semantic = os.environ.get("BINDERY_SEMANTIC", "").strip().lower()
        resolved_semantic = (
            semantic
            if semantic is not None
            else env_semantic not in {"0", "off", "false", "no"}
        )
        return cls(
            vault=resolved_vault.resolve(),
            state_dir=resolved_state,
            max_tokens=max_tokens or _env_int("BINDERY_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            limit=limit or _env_int("BINDERY_LIMIT", DEFAULT_LIMIT),
            chunk_tokens=_env_int("BINDERY_CHUNK_TOKENS", DEFAULT_CHUNK_TOKENS),
            chunk_overlap=_env_int("BINDERY_CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP),
            semantic=resolved_semantic,
            autocapture=env_capture not in {"0", "off", "false", "no"},
            episodes=env_episodes not in {"0", "off", "false", "no"},
            include=_clean_prefixes(include) if include else _env_list("BINDERY_INCLUDE"),
            exclude=_clean_prefixes(exclude) if exclude else _env_list("BINDERY_EXCLUDE"),
            project=(
                project
                if project is not None
                else detect_project(state_dir=resolved_state)
            ),
        )


def _slugify(text: str) -> str:
    """Stable, filesystem-safe key for a vault path."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
