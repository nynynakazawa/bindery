"""Which project a directory belongs to.

Every case here is a way one body of work got split into several memories, or
a directory claimed an identity that matched nothing at all.
"""

from __future__ import annotations

import subprocess

import pytest

from bindery.cli import main
from bindery.workspace import MARKER, load_registry, resolve, save_registry


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("BINDERY_PROJECT", raising=False)


def _tree(tmp_path, *parts):
    target = tmp_path.joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    return target


# ------------------------------------------------------------------ the bug


def test_subdirectories_of_a_workspace_share_its_project(tmp_path, state):
    """The failure: one project becoming three, by where the editor was opened."""
    root = _tree(tmp_path, "Zidainnovation", "Gakuwari")
    sales = _tree(tmp_path, "Zidainnovation", "Gakuwari", "Sales")
    deep = _tree(tmp_path, "Zidainnovation", "Gakuwari", "Sales", "app", "src")
    save_registry(state, [{"name": "gakuwari", "path": str(root)}])

    for directory in (root, sales, deep):
        assert resolve(directory, state_dir=state).name == "gakuwari"


def test_a_registered_workspace_beats_an_inner_git_repository(tmp_path, state):
    """`.../Gakuwari/Sales` being its own repo made it a project called "Sales"."""
    root = _tree(tmp_path, "work")
    sales = _tree(tmp_path, "work", "Sales")
    subprocess.run(["git", "init", "-q", str(sales)], check=True, capture_output=True)
    save_registry(state, [{"name": "gakuwari", "path": str(root)}])

    assert resolve(sales, state_dir=state).name == "gakuwari"


def test_the_most_specific_registration_wins(tmp_path, state):
    outer = _tree(tmp_path, "workspace")
    inner = _tree(tmp_path, "workspace", "separate-product")
    save_registry(state, [
        {"name": "workspace", "path": str(outer)},
        {"name": "separate", "path": str(inner)},
    ])

    assert resolve(outer, state_dir=state).name == "workspace"
    assert resolve(inner / "deep", state_dir=state).name == "separate"


def test_a_marker_file_travels_with_the_repository(tmp_path, state):
    root = _tree(tmp_path, "repo")
    (root / MARKER).write_text("myproject\n", encoding="utf-8")

    assert resolve(root / "src" / "deep", state_dir=state).name == "myproject"


def test_an_empty_marker_names_its_own_directory(tmp_path, state):
    """Enough to stop a subdirectory claiming a separate identity."""
    root = _tree(tmp_path, "repo")
    (root / MARKER).write_text("", encoding="utf-8")

    assert resolve(root / "src", state_dir=state).name == "repo"


def test_the_nearest_marker_wins(tmp_path, state):
    """A package declaring itself a project knows something the root does not."""
    root = _tree(tmp_path, "mono")
    package = _tree(tmp_path, "mono", "packages", "api")
    (root / MARKER).write_text("mono\n", encoding="utf-8")
    (package / MARKER).write_text("api\n", encoding="utf-8")

    assert resolve(package, state_dir=state).name == "api"
    assert resolve(root, state_dir=state).name == "mono"


def test_a_marker_beats_the_registry(tmp_path, state):
    root = _tree(tmp_path, "repo")
    (root / MARKER).write_text("declared\n", encoding="utf-8")
    save_registry(state, [{"name": "registered", "path": str(root)}])

    assert resolve(root, state_dir=state).name == "declared"


# ------------------------------------------------------------- fallbacks


def test_the_environment_overrides_everything(tmp_path, state, monkeypatch):
    root = _tree(tmp_path, "repo")
    (root / MARKER).write_text("declared\n", encoding="utf-8")
    monkeypatch.setenv("BINDERY_PROJECT", "forced")

    assert resolve(root, state_dir=state).name == "forced"


def test_an_empty_environment_value_turns_scoping_off(tmp_path, state, monkeypatch):
    """Deliberately global, not a failure to detect anything."""
    monkeypatch.setenv("BINDERY_PROJECT", "")
    assert resolve(tmp_path, state_dir=state).name == ""


def test_an_unregistered_directory_still_gets_a_name(tmp_path, state):
    root = _tree(tmp_path, "loose")
    assert resolve(root, state_dir=state).name == "loose"


def test_resolution_says_where_the_name_came_from(tmp_path, state):
    """"Why does this directory think it is called Sales" has to be answerable."""
    root = _tree(tmp_path, "repo")
    assert resolve(root, state_dir=state).source == "directory name"

    save_registry(state, [{"name": "named", "path": str(root)}])
    assert resolve(root, state_dir=state).source == "registry"

    (root / MARKER).write_text("declared\n", encoding="utf-8")
    assert resolve(root, state_dir=state).source == "marker file"


def test_a_corrupt_registry_does_not_break_resolution(tmp_path, state):
    (state / "projects.json").write_text("{not json", encoding="utf-8")
    root = _tree(tmp_path, "loose")
    assert resolve(root, state_dir=state).name == "loose"


# ------------------------------------------------------------------- CLI


def test_project_add_registers_a_directory(tmp_path, state, capsys):
    root = _tree(tmp_path, "work")
    main(["project", "add", "gakuwari", str(root), "--state-dir", str(state)])

    assert load_registry(state) == [{"name": "gakuwari", "path": str(root.resolve())}]
    assert resolve(root / "deep", state_dir=state).name == "gakuwari"


def test_project_add_can_write_the_marker_too(tmp_path, state):
    root = _tree(tmp_path, "work")
    main(["project", "add", "gakuwari", str(root), "--marker", "--state-dir", str(state)])

    assert (root / MARKER).read_text(encoding="utf-8").strip() == "gakuwari"


def test_registering_the_same_directory_twice_replaces_it(tmp_path, state):
    root = _tree(tmp_path, "work")
    main(["project", "add", "old", str(root), "--state-dir", str(state)])
    main(["project", "add", "new", str(root), "--state-dir", str(state)])

    assert [e["name"] for e in load_registry(state)] == ["new"]


def test_project_remove(tmp_path, state):
    root = _tree(tmp_path, "work")
    main(["project", "add", "gakuwari", str(root), "--state-dir", str(state)])
    main(["project", "remove", "gakuwari", "--state-dir", str(state)])

    assert load_registry(state) == []


def test_project_which_explains_itself(tmp_path, state, capsys):
    root = _tree(tmp_path, "work")
    main(["project", "add", "gakuwari", str(root), "--state-dir", str(state)])
    capsys.readouterr()

    main(["project", "which", str(root / "deep"), "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert "gakuwari" in out and "registry" in out


def test_a_home_directory_is_not_a_project(tmp_path, state, monkeypatch):
    """It named itself, matched nothing, and every search silently widened."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    result = resolve(fake_home, state_dir=state)
    assert result.name == ""
    assert "not a project" in result.source


def test_a_registered_home_still_wins(tmp_path, state, monkeypatch):
    """The rule is a fallback, not a veto on what the user said."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    save_registry(state, [{"name": "personal", "path": str(fake_home)}])

    assert resolve(fake_home, state_dir=state).name == "personal"
