"""Automatic session capture (C) and client-neutral setup."""

import json
import time

from bindery.config import Config
from bindery.growth import AUTO_CAPTURE_MIN_SIGNALS, SESSION_PREFIX, SessionRecord, promotion_candidates
from bindery.server import MemoryServer
from bindery.store import Store


def _call(server, name, args=None):
    """Invoke a tool the way the MCP layer does, without the transport."""
    return server.call_tool(name, args or {})


def _sessions_dir(config):
    return config.vault / SESSION_PREFIX


# ------------------------------------------------------------ threshold --


def test_signals_count_misses_and_writes_not_successful_searches():
    """A session that found everything it needed taught the system nothing."""
    quiet = SessionRecord(started=0, ended=1, searches=20)
    assert quiet.signals == 0
    assert not quiet.worth_recording()

    productive = SessionRecord(started=0, ended=1, searches=2, unanswered=["a"], written=["b.md"])
    assert productive.signals == AUTO_CAPTURE_MIN_SIGNALS
    assert productive.worth_recording()


def test_a_single_signal_is_below_the_threshold():
    assert not SessionRecord(started=0, ended=1, unanswered=["only one"]).worth_recording()


def test_routine_lookup_session_writes_nothing(config):
    (config.vault / "a.md").write_text("# A\n\nalpha content\n", encoding="utf-8")
    server = MemoryServer(config)
    for _ in range(5):
        _call(server, "memory_search", {"query": "alpha"})

    assert server.finalize_session() is None
    assert not _sessions_dir(config).exists()


def test_session_with_unanswered_questions_is_recorded(config):
    server = MemoryServer(config)
    _call(server, "memory_search", {"query": "デプロイ手順"})
    _call(server, "memory_search", {"query": "オンコール体制"})

    rel = server.finalize_session()
    assert rel and rel.startswith(SESSION_PREFIX)

    body = (config.vault / rel).read_text(encoding="utf-8")
    assert "デプロイ手順" in body and "オンコール体制" in body
    assert "Asked, not answered" in body


def test_written_notes_appear_in_the_session_record(config):
    server = MemoryServer(config)
    _call(server, "memory_write", {"path": "adr/a.md", "content": "決定A。"})
    _call(server, "memory_write", {"path": "adr/b.md", "content": "決定B。"})

    rel = server.finalize_session()
    body = (config.vault / rel).read_text(encoding="utf-8")
    assert "[[a]]" in body and "[[b]]" in body


def test_finalize_is_idempotent(config):
    server = MemoryServer(config)
    _call(server, "memory_search", {"query": "missing one"})
    _call(server, "memory_search", {"query": "missing two"})

    first = server.finalize_session()
    assert first is not None
    assert server.finalize_session() is None

    body = (config.vault / first).read_text(encoding="utf-8")
    assert body.count("Asked, not answered") == 1


def test_repeated_sessions_append_to_one_daily_file(config):
    for _ in range(2):
        server = MemoryServer(config)
        _call(server, "memory_search", {"query": "gap one"})
        _call(server, "memory_search", {"query": "gap two"})
        server.finalize_session()

    files = list(_sessions_dir(config).glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").count("Asked, not answered") == 2


def test_autocapture_can_be_switched_off(config):
    disabled = Config.resolve(vault=config.vault, state_dir=config.state_dir, semantic=False)
    object.__setattr__(disabled, "autocapture", False)
    server = MemoryServer(disabled)
    _call(server, "memory_search", {"query": "missing one"})
    _call(server, "memory_search", {"query": "missing two"})
    assert server.finalize_session() is None


def test_session_records_do_not_become_promotion_candidates(config):
    """Machine-written activity logs must not compete with real topics."""
    sessions = _sessions_dir(config)
    sessions.mkdir(parents=True, exist_ok=True)
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        (sessions / f"{day}.md").write_text(
            f"---\ntitle: Sessions {day}\ntags: [session]\n---\n\n# Sessions {day}\n\nbody\n",
            encoding="utf-8",
        )
    store = Store(config.db_path)
    from bindery.indexer import reindex

    reindex(config, store)
    assert "session" not in {c.tag for c in promotion_candidates(store)}


# ------------------------------------------------------ client neutrality --


def test_install_covers_every_client_by_default(capsys, config):
    from bindery.cli import build_parser, main

    main(["install", "--vault", str(config.vault)])
    out = capsys.readouterr().out
    assert "Claude Code" in out and "Codex" in out
    assert "mcpServers" in out                      # Claude form
    assert "[mcp_servers.bindery]" in out      # Codex form
    assert str(config.vault) in out


def test_install_can_target_one_client(capsys, config):
    from bindery.cli import main

    main(["install", "codex", "--vault", str(config.vault)])
    out = capsys.readouterr().out
    assert "[mcp_servers.bindery]" in out
    assert "mcpServers" not in out


def test_install_write_creates_codex_config(tmp_path, monkeypatch, config, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from bindery.cli import main

    main(["install", "codex", "--write", "--vault", str(config.vault)])
    written = tmp_path / ".codex" / "config.toml"
    assert written.exists()
    assert "[mcp_servers.bindery]" in written.read_text(encoding="utf-8")


def test_install_write_does_not_duplicate_codex_block(tmp_path, monkeypatch, config, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from bindery.cli import main

    main(["install", "codex", "--write", "--vault", str(config.vault)])
    main(["install", "codex", "--write", "--vault", str(config.vault)])
    body = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert body.count("[mcp_servers.bindery]") == 1


def test_install_write_updates_a_stale_codex_command(tmp_path, monkeypatch, config, capsys):
    """Refusing to update our own block strands the old path when the binary moves."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    conf = tmp_path / ".codex" / "config.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        '[mcp_servers.other]\ncommand = "keep-me"\n\n'
        '[mcp_servers.bindery]\ncommand = "/gone/old/path"\nargs = ["serve"]\n\n'
        '[mcp_servers.bindery.env]\nBINDERY_VAULT = "/old/vault"\n',
        encoding="utf-8",
    )
    from bindery.cli import main

    main(["install", "codex", "--write", "--vault", str(config.vault)])
    body = conf.read_text(encoding="utf-8")

    assert "/gone/old/path" not in body
    assert "/old/vault" not in body
    assert body.count("[mcp_servers.bindery]") == 1
    assert "keep-me" in body                       # other servers survive
    assert str(config.vault) in body
    assert "updated" in capsys.readouterr().out


def test_install_write_preserves_other_codex_servers(tmp_path, monkeypatch, config, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    existing = tmp_path / ".codex" / "config.toml"
    existing.parent.mkdir(parents=True)
    existing.write_text('[mcp_servers.other]\ncommand = "keep-me"\n', encoding="utf-8")
    from bindery.cli import main

    main(["install", "codex", "--write", "--vault", str(config.vault)])
    body = existing.read_text(encoding="utf-8")
    assert "keep-me" in body and "[mcp_servers.bindery]" in body
    assert (tmp_path / ".codex" / "config.toml.bak").exists()


def test_install_write_preserves_claude_application_state(tmp_path, monkeypatch, config, capsys):
    """~/.claude.json is live app state, not a config file someone wrote."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".claude.json").write_text(
        json.dumps({"projects": {"/some/repo": {"history": [1, 2, 3]}},
                    "mcpServers": {"other": {"command": "keep-me"}}}),
        encoding="utf-8",
    )
    from bindery.cli import main

    main(["install", "claude", "--write", "--vault", str(config.vault)])
    payload = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert payload["projects"]["/some/repo"]["history"] == [1, 2, 3]
    assert "other" in payload["mcpServers"]
    assert "bindery" in payload["mcpServers"]
    assert (tmp_path / ".claude.json.bak").exists()


def test_install_project_scope_writes_the_project_file(tmp_path, monkeypatch, config, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    from bindery.cli import main

    main(["install", "claude", "--write", "--local", "--vault", str(config.vault)])
    assert (tmp_path / ".mcp.json").exists()


def test_both_agents_default_to_the_same_scope(config):
    """The asymmetry this guards against: one agent global, the other per-project."""
    from bindery.cli import _client_targets

    targets = _client_targets(config)
    assert {spec["scope"] for spec in targets.values()} == {"user"}


def test_server_command_prefers_the_running_installation(tmp_path, monkeypatch):
    """PATH can point at a different copy than the one being installed."""
    import bindery.cli as cli

    running = tmp_path / "uvtool" / "bin"
    running.mkdir(parents=True)
    (running / "python").touch()
    (running / "bindery").touch()
    stale = tmp_path / "other" / "bindery"
    stale.parent.mkdir(parents=True)
    stale.touch()

    monkeypatch.setattr(cli.sys, "executable", str(running / "python"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(stale))

    command, extra = cli._server_command()
    assert command == str(running / "bindery")
    assert extra == []


def test_server_command_prefers_the_path_name_for_the_same_file(tmp_path, monkeypatch):
    """A symlink on PATH is the stable name for the same install - use it."""
    import bindery.cli as cli

    running = tmp_path / "uvtool" / "bin"
    running.mkdir(parents=True)
    (running / "python").touch()
    real = running / "bindery"
    real.touch()
    link = tmp_path / "bin" / "bindery"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    monkeypatch.setattr(cli.sys, "executable", str(running / "python"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(link))

    assert cli._server_command()[0] == str(link)


def test_server_command_falls_back_to_module_execution(tmp_path, monkeypatch):
    """No console script anywhere: `python -m bindery` still starts the server."""
    import bindery.cli as cli

    interpreter = tmp_path / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(cli.sys, "executable", str(interpreter))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    assert cli._server_command() == (str(interpreter), ["-m", "bindery"])


def test_prompt_block_is_client_neutral(capsys):
    from bindery.cli import main

    main(["prompt"])
    out = capsys.readouterr().out
    assert "memory_search" in out and "memory_learn" in out
    # It must read as instructions for any agent, not for one named product.
    assert "Claude" not in out and "Codex" not in out


def test_prompt_write_appends_to_both_agent_docs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from bindery.cli import main

    main(["prompt", "--write"])
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert "Shared memory (Bindery)" in (tmp_path / name).read_text(encoding="utf-8")


def test_prompt_write_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from bindery.cli import main

    main(["prompt", "--write"])
    main(["prompt", "--write"])
    body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert body.count("Shared memory (Bindery)") == 1


def test_a_recorded_gap_does_not_answer_the_question_it_records(config):
    """The defect this guards against is subtle and severe.

    A session record lists the questions that had no answer. If those records
    are retrievable, the second time anyone asks, the record itself matches -
    the gap looks answered and vanishes from review, precisely because it was
    detected once.
    """
    first = MemoryServer(config)
    _call(first, "memory_search", {"query": "オンコール体制"})
    _call(first, "memory_search", {"query": "SLO の設定値"})
    assert first.finalize_session() is not None

    second = MemoryServer(config)
    text = _call(second, "memory_search", {"query": "オンコール体制"})
    assert "No passages matched" in text
    assert second.session.unanswered == ["オンコール体制"]


def test_session_records_stay_readable_on_disk(config):
    """Excluded from retrieval, still present for a human in Obsidian."""
    server = MemoryServer(config)
    _call(server, "memory_search", {"query": "gap one"})
    _call(server, "memory_search", {"query": "gap two"})
    rel = server.finalize_session()
    assert (config.vault / rel).exists()

    store = Store(config.db_path)
    indexed = {row["path"] for row in store.conn.execute("SELECT path FROM notes")}
    assert rel not in indexed


def test_prompt_write_user_scope_targets_both_agent_homes(tmp_path, monkeypatch, capsys):
    """A vault shared across projects needs instructions that are too."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    from bindery.cli import main

    main(["prompt", "--write", "--global"])
    assert "Shared memory (Bindery)" in (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Shared memory (Bindery)" in (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")


def test_prompt_write_user_scope_backs_up_existing_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    existing = tmp_path / ".claude" / "CLAUDE.md"
    existing.write_text("# My own rules\n\nDo not break these.\n", encoding="utf-8")
    from bindery.cli import main

    main(["prompt", "--write", "--global"])
    body = existing.read_text(encoding="utf-8")
    assert "Do not break these." in body            # original policy preserved
    assert "Shared memory (Bindery)" in body   # block appended
    assert (tmp_path / ".claude" / "CLAUDE.md.bak").exists()


def test_prompt_write_skips_agents_that_are_not_installed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    from bindery.cli import main

    main(["prompt", "--write", "--global"])
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".codex").exists()
    assert "skipped" in capsys.readouterr().out


def test_setup_is_a_dry_run_by_default(tmp_path, monkeypatch, capsys, config):
    """setup touches hand-maintained policy files, so it must not act uninvited."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    from bindery.cli import main

    main(["setup", "--vault", str(config.vault)])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / ".claude.json").exists()


def test_setup_write_completes_every_step(tmp_path, monkeypatch, capsys, config):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (config.vault / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    from bindery.cli import main

    main(["setup", "--write", "--vault", str(config.vault)])

    # server configuration, for both agents
    assert "[mcp_servers.bindery]" in (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "bindery" in (tmp_path / ".claude.json").read_text(encoding="utf-8")
    # and the instructions, without which nothing is ever recorded
    assert "Shared memory (Bindery)" in (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Shared memory (Bindery)" in (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")


def test_setup_write_is_idempotent(tmp_path, monkeypatch, capsys, config):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    from bindery.cli import main

    main(["setup", "--write", "--vault", str(config.vault)])
    main(["setup", "--write", "--vault", str(config.vault)])

    toml = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    claude_md = (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert toml.count("[mcp_servers.bindery]") == 1
    assert claude_md.count("Shared memory (Bindery)") == 1


def test_install_points_at_the_global_prompt_command(capsys, config):
    """The hint must not send people to the project-scoped command."""
    from bindery.cli import main

    main(["install", "--vault", str(config.vault)])
    out = capsys.readouterr().out
    assert "prompt --global --write" in out
    assert "setup --write" in out
