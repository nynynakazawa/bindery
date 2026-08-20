"""Configuration resolution.

Every setting has a working default so that `python3 -m bindery serve`
does something sensible with no arguments. Environment variables override the
defaults; explicit CLI flags override the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(slots=True)
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
        )


def _slugify(text: str) -> str:
    """Stable, filesystem-safe key for a vault path."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
