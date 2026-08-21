"""Deciding which project a directory belongs to.

The working directory is the only signal an MCP server gets about what the
agent is working on, and reading it naively splits one body of work into
several memories. Three ways that happened here:

    ~/Zidainnovation              -> "Zidainnovation"
    ~/Zidainnovation/Gakuwari     -> "Gakuwari"
    ~/Zidainnovation/Gakuwari/Sales -> "Sales"

Three project names for one project, decided by which folder the editor
happened to be opened at. Nothing nests, so a decision recorded while the
editor was open one level up is invisible one level down. And a directory that
is nobody's project - a home directory, where the desktop app starts - gets its
own name too, matching no notes at all, which is why every search from there
fell back to the whole vault.

So the boundary is something the user states rather than something inferred
from the shape of the filesystem, with inference kept only as the fallback:

    1. BINDERY_PROJECT               - explicit, for one process
    2. a .bindery-project marker     - explicit, travels with the repository
    3. the workspace registry        - explicit, central, nothing added to trees
    4. the git repository            - inferred
    5. the directory name            - inferred, last resort
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Dropped in a directory to name the project it and everything under it
#: belongs to. An empty file means "use this directory's name", which is
#: enough to stop a subdirectory from claiming its own identity.
MARKER = ".bindery-project"

#: Central registry, for naming workspaces without putting files in them.
REGISTRY = "projects.json"


@dataclass(slots=True)
class Resolution:
    """A project name and how it was arrived at.

    The provenance is not decoration: "why does this directory think it is
    called Sales" is the question this module exists to answer, and it should
    be answerable without reading the source.
    """

    name: str
    source: str
    origin: str = ""

    def describe(self) -> str:
        where = f" ({self.origin})" if self.origin else ""
        return f"{self.name or '(none)'} - from {self.source}{where}"


def registry_path(state_dir: Path) -> Path:
    return Path(state_dir) / REGISTRY


def load_registry(state_dir: Path) -> list[dict]:
    path = registry_path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    entries = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("name") and entry.get("path")
    ]


def save_registry(state_dir: Path, entries: list[dict]) -> None:
    path = registry_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(entries, key=lambda e: str(e["path"]))
    path.write_text(
        json.dumps({"projects": ordered}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _from_marker(start: Path) -> Resolution | None:
    """The nearest marker walking upwards.

    Nearest rather than outermost: a marker inside a monorepo package is a
    deliberate statement that the package is its own project, and the one at
    the root cannot know that.
    """
    for directory in [start, *start.parents]:
        marker = directory / MARKER
        if not marker.is_file():
            continue
        try:
            declared = marker.read_text(encoding="utf-8").strip()
        except OSError:
            declared = ""
        name = declared.splitlines()[0].strip() if declared else directory.name
        return Resolution(name, "marker file", str(marker))
    return None


def _from_registry(start: Path, state_dir: Path) -> Resolution | None:
    """The most specific registered directory containing ``start``.

    Longest match wins, so registering both a workspace and one project inside
    it does the obvious thing rather than depending on file order.
    """
    best: tuple[int, dict] | None = None
    for entry in load_registry(state_dir):
        try:
            root = Path(entry["path"]).expanduser().resolve()
        except OSError:
            continue
        if _is_within(start, root):
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, entry)
    if best is None:
        return None
    return Resolution(str(best[1]["name"]), "registry", str(best[1]["path"]))


def _from_git(start: Path) -> Resolution | None:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(start), *args],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    url = run("remote", "get-url", "origin")
    if url:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        return Resolution(name[:-4] if name.endswith(".git") else name, "git remote", url)
    # A repository with no remote still has a root, and the root is a far
    # better boundary than the subdirectory the agent happened to start in.
    top = run("rev-parse", "--show-toplevel")
    if top:
        return Resolution(Path(top).name, "git repository", top)
    return None


def resolve(start: Path | None = None, *, state_dir: Path | None = None) -> Resolution:
    """Name the project a directory belongs to, and say how that was decided."""
    override = os.environ.get("BINDERY_PROJECT")
    if override is not None and override.strip():
        return Resolution(override.strip(), "BINDERY_PROJECT")
    if override is not None:
        # Explicitly empty: scoping off, and that is a decision, not a failure.
        return Resolution("", "BINDERY_PROJECT")

    directory = Path(start) if start else Path.cwd()
    try:
        directory = directory.resolve()
    except OSError:  # pragma: no cover - unreadable cwd
        return Resolution(directory.name, "directory name")

    marker = _from_marker(directory)
    if marker:
        return marker
    if state_dir is not None:
        registered = _from_registry(directory, Path(state_dir).expanduser())
        if registered:
            return registered
    from_git = _from_git(directory)
    if from_git:
        return from_git
    return Resolution(directory.name, "directory name", str(directory))
