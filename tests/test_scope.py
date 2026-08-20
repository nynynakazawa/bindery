"""Project boundaries and the index allowlist.

Both answer the same question - what is this search allowed to see - and both
exist because the answer used to be "everything in the vault", which is wrong
in two different ways: another repository's decision is not evidence about
this one, and a personal vault holds far more than work notes.
"""

from __future__ import annotations

import json

import pytest

from bindery.config import Config
from bindery.server import MemoryServer


def _call(server, name, args):
    """Invoke a tool the way the MCP layer does, without the transport."""
    try:
        return server.call_tool(name, args), False
    except Exception as exc:  # mirrors what the SDK reports back to the model
        return f"{type(exc).__name__}: {exc}", True


@pytest.fixture
def vault(tmp_path):
    directory = tmp_path / "vault"
    directory.mkdir()
    return directory


def _config(vault, tmp_path, project="alpha", **kwargs):
    return Config.resolve(
        vault=vault, state_dir=tmp_path / "state", semantic=False,
        project=project, **kwargs,
    )


def _note(vault, rel: str, body: str, project: str | None = None) -> None:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    front = f"---\nproject: {project}\n---\n\n" if project is not None else ""
    target.write_text(f"{front}# Note\n\n{body}\n", encoding="utf-8")


# ------------------------------------------------------------- project scope


def test_another_projects_decision_does_not_answer_this_project(vault, tmp_path):
    """The failure this prevents: 'we use Clerk' surfacing in the Firebase repo."""
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    _note(vault, "beta/auth.md", "認証は Clerk を使う。", project="beta")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, error = _call(server, "memory_search", {"query": "認証"})

    assert not error
    assert "Firebase" in text
    assert "Clerk" not in text


def test_cross_project_notes_are_visible_from_every_project(vault, tmp_path):
    """Knowledge that is not about one codebase must not be trapped in one."""
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    _note(vault, "conventions.md", "秘密情報はログに出さない。", project="")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "ログ"})
    assert "秘密情報はログに出さない" in text


def test_scope_all_crosses_the_boundary_on_request(vault, tmp_path):
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    _note(vault, "beta/auth.md", "認証は Clerk を使う。", project="beta")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "認証", "scope": "all"})
    assert "Firebase" in text and "Clerk" in text


def test_an_empty_project_scope_widens_rather_than_reporting_nothing(vault, tmp_path):
    """Narrowing must never be the reason an answer is missed."""
    _note(vault, "beta/auth.md", "認証は Clerk を使う。", project="beta")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "認証"})

    # The answer is returned - but attributed, not passed off as this project's.
    assert "Clerk" in text
    assert "[beta]" in text
    assert "Nothing in project 'alpha' matched" in text


def test_a_partial_scoped_search_says_more_exists_elsewhere(vault, tmp_path):
    """When the project does answer, other projects stay out - but are announced."""
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    _note(vault, "beta/auth.md", "認証は Clerk を使う。", project="beta")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "認証"})

    assert "Firebase" in text
    assert "Clerk" not in text
    assert 'scope="all"' in text
    assert "other projects" in text


def test_no_widening_hint_when_nothing_is_hidden(vault, tmp_path):
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "認証"})
    assert 'scope="all"' not in text


def test_results_name_the_project_they_came_from(vault, tmp_path):
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    _note(vault, "conventions.md", "認証情報はログに出さない。", project="")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "認証", "scope": "all"})
    assert "[alpha]" in text and "[global]" in text


def test_writes_record_the_current_project(vault, tmp_path):
    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    _call(server, "memory_write", {"path": "adr/cache.md", "content": "Redis は使わない。"})

    body = (vault / "adr" / "cache.md").read_text(encoding="utf-8")
    assert "project: alpha" in body
    # and it round-trips through a scoped search
    text, _ = _call(server, "memory_search", {"query": "Redis"})
    assert "Redis は使わない" in text


def test_a_write_can_opt_out_of_the_project(vault, tmp_path):
    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    _call(server, "memory_write", {"path": "style.md", "content": "早期returnを好む。", "project": ""})

    body = (vault / "style.md").read_text(encoding="utf-8")
    assert "project:" not in body

    other = MemoryServer(_config(vault, tmp_path, project="beta"))
    text, _ = _call(other, "memory_search", {"query": "早期return"})
    assert "早期return" in text


def test_journals_are_kept_per_project(vault, tmp_path):
    """One global journal per day is the one file a scoped search cannot filter."""
    alpha = MemoryServer(_config(vault, tmp_path, project="alpha"))
    _call(alpha, "memory_learn", {"content": "alpha の学び。", "tags": ["x"]})
    beta = MemoryServer(_config(vault, tmp_path, project="beta"))
    _call(beta, "memory_learn", {"content": "beta の学び。", "tags": ["y"]})

    assert list((vault / "journal" / "alpha").glob("*.md"))
    assert list((vault / "journal" / "beta").glob("*.md"))

    text, _ = _call(alpha, "memory_search", {"query": "学び"})
    assert "alpha の学び" in text
    assert "beta の学び" not in text


def test_scoping_is_off_when_no_project_can_be_identified(vault, tmp_path):
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。", project="alpha")
    _note(vault, "beta/auth.md", "認証は Clerk を使う。", project="beta")

    server = MemoryServer(_config(vault, tmp_path, project=""))
    text, _ = _call(server, "memory_search", {"query": "認証"})
    assert "Firebase" in text and "Clerk" in text


def test_project_falls_back_to_the_directory(vault, tmp_path):
    """Notes filed one folder per project work with no front matter at all."""
    _note(vault, "alpha/auth.md", "認証は Firebase Auth を使う。")
    _note(vault, "beta/auth.md", "認証は Clerk を使う。")

    server = MemoryServer(_config(vault, tmp_path, project="alpha"))
    text, _ = _call(server, "memory_search", {"query": "認証"})
    assert "Firebase" in text and "Clerk" not in text


# ---------------------------------------------------------- index boundary


def test_include_is_an_allowlist(vault, tmp_path):
    """Everything indexed can reach a hosted model, so opting in beats opting out."""
    _note(vault, "work/api.md", "レート制限は 100rpm。", project="")
    _note(vault, "日記/2026.md", "今日は体調が悪かった。", project="")

    server = MemoryServer(_config(vault, tmp_path, project="", include=["work"]))
    text, _ = _call(server, "memory_search", {"query": "体調"})
    assert "悪かった" not in text

    text, _ = _call(server, "memory_search", {"query": "レート制限"})
    assert "100rpm" in text


def test_exclude_removes_a_subtree(vault, tmp_path):
    _note(vault, "work/api.md", "レート制限は 100rpm。", project="")
    _note(vault, "private/health.md", "通院の記録。", project="")

    server = MemoryServer(_config(vault, tmp_path, project="", exclude=["private"]))
    text, _ = _call(server, "memory_search", {"query": "通院"})
    assert "通院の記録" not in text


def test_a_prefix_does_not_match_a_longer_name(vault, tmp_path):
    """'private' must not be taken to cover 'private-api-notes.md'."""
    _note(vault, "private-api-notes.md", "レート制限は 100rpm。", project="")
    _note(vault, "private/health.md", "通院の記録。", project="")

    server = MemoryServer(_config(vault, tmp_path, project="", exclude=["private"]))
    text, _ = _call(server, "memory_search", {"query": "レート制限"})
    assert "100rpm" in text


def test_the_boundary_also_covers_reading_by_path(vault, tmp_path):
    """An allowlist that only filters search is not a boundary."""
    _note(vault, "private/health.md", "通院の記録。", project="")

    server = MemoryServer(_config(vault, tmp_path, project="", exclude=["private"]))
    text, _ = _call(server, "memory_read", {"path": "private/health.md"})

    assert "通院の記録" not in text
    assert "Refused" in text


def test_the_boundary_also_covers_writing(vault, tmp_path):
    server = MemoryServer(_config(vault, tmp_path, project="", exclude=["private"]))
    text, _ = _call(server, "memory_write", {"path": "private/leak.md", "content": "x"})

    assert "Refused" in text
    assert not (vault / "private" / "leak.md").exists()


def test_status_reports_the_boundary_in_force(vault, tmp_path):
    _note(vault, "work/api.md", "body", project="")
    server = MemoryServer(_config(vault, tmp_path, project="alpha", include=["work"]))
    status = json.loads(_call(server, "memory_status", {})[0])

    assert status["project"] == "alpha"
    assert status["indexed_only"] == ["work"]


# ------------------------------------------------- the boundary must persist


def test_the_boundary_is_written_into_the_agent_configuration(vault, tmp_path, monkeypatch):
    """Otherwise it covers the index built at setup time and nothing after it."""
    import json as _json

    from bindery.cli import main

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _note(vault, "work/api.md", "body", project="")

    main([
        "install", "--write", "--vault", str(vault),
        "--include", "work", "--exclude", "private",
    ])

    config = _json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    env = config["mcpServers"]["bindery"]["env"]
    assert env["BINDERY_INCLUDE"] == "work"
    assert env["BINDERY_EXCLUDE"] == "private"

    codex = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'BINDERY_INCLUDE = "work"' in codex
    assert 'BINDERY_EXCLUDE = "private"' in codex


def test_no_boundary_means_no_extra_environment(vault, tmp_path, monkeypatch):
    """A whole-vault install should not carry empty settings around."""
    import json as _json

    from bindery.cli import main

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    _note(vault, "work/api.md", "body", project="")

    main(["install", "--write", "--vault", str(vault)])

    config = _json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert set(config["mcpServers"]["bindery"]["env"]) == {"BINDERY_VAULT"}


def test_the_environment_boundary_is_honoured_by_the_server(vault, tmp_path, monkeypatch):
    """The env var the config carries has to actually restrict the index."""
    _note(vault, "work/api.md", "レート制限は 100rpm。", project="")
    _note(vault, "日記/2026.md", "今日は体調が悪かった。", project="")

    monkeypatch.setenv("BINDERY_INCLUDE", "work")
    config = Config.resolve(vault=vault, state_dir=tmp_path / "state", semantic=False, project="")
    server = MemoryServer(config)

    text, _ = _call(server, "memory_search", {"query": "体調", "scope": "all"})
    assert "悪かった" not in text


def test_an_indexed_container_directory_is_not_the_project(vault, tmp_path):
    """`work/alpha` and `work/beta` are two projects, not one called "work"."""
    _note(vault, "work/alpha/auth.md", "認証は Firebase Auth を使う。")
    _note(vault, "work/beta/auth.md", "認証は Clerk を使う。")

    server = MemoryServer(_config(vault, tmp_path, project="alpha", include=["work"]))
    text, _ = _call(server, "memory_search", {"query": "認証"})

    assert "Firebase" in text
    assert "Clerk" not in text
    assert "[alpha]" in text
