"""Every place an agent might look for an MCP server.

One shared memory only works if every agent on the machine is pointed at it.
Missing one is not a partial success - a Codex account with no registration
starts every session with nothing, and the sessions that do have the memory
never learn what it failed to record.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from bindery.cli import _client_targets, _detect_clients, main
from bindery.config import Config


@pytest.fixture
def config(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n\n本文。\n", encoding="utf-8")
    return Config.resolve(
        vault=vault, state_dir=tmp_path / "state", semantic=False, project="alpha"
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home with every client already present."""
    root = tmp_path / "home"
    (root / ".claude").mkdir(parents=True)
    (root / ".codex").mkdir(parents=True)
    (root / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (root / ".cursor").mkdir(parents=True)
    (root / "Library" / "Application Support" / "Code" / "User").mkdir(parents=True)
    (root / ".codex-homes" / "codexb").mkdir(parents=True)
    (root / ".codex-homes" / "codexc").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: root)
    monkeypatch.setattr("sys.platform", "darwin")
    return root


def _vscode(home):
    return home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"


# ------------------------------------------------------------------ coverage


def test_every_client_on_the_machine_is_a_target(config, home):
    targets = _client_targets(config)
    assert set(targets) == {
        "claude", "antigravity", "codex", "vscode", "cursor",
        "codex:codexb", "codex:codexc",
    }


def test_each_codex_account_gets_its_own_registration(config, home):
    """codex-multi gives each account its own CODEX_HOME.

    A server registered in ~/.codex is invisible from all of them, so a
    background Codex run would start with no shared memory at all.
    """
    targets = _client_targets(config)
    assert targets["codex:codexb"]["path"] == home / ".codex-homes" / "codexb" / "config.toml"
    assert targets["codex:codexc"]["path"] == home / ".codex-homes" / "codexc" / "config.toml"


def test_absent_clients_are_not_offered(config, tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: bare)
    assert "cursor" not in _detect_clients()
    assert not any(name.startswith("codex:") for name in _client_targets(config))


def test_one_file_covers_the_claude_app_cli_and_extension(config, home):
    """They read the same file; three entries would be three ways to drift."""
    assert _client_targets(config)["claude"]["path"] == home / ".claude.json"


def test_antigravity_uses_its_global_mcp_profile(config, home):
    assert _client_targets(config)["antigravity"]["path"] == (
        home / ".gemini" / "config" / "mcp_config.json"
    )


def test_antigravity_detects_the_agy_executable(config, home, monkeypatch):
    import bindery.cli as cli

    (home / ".gemini" / "antigravity-cli").rmdir()
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/opt/homebrew/bin/agy" if name == "agy" else None
    )
    assert "antigravity" in _detect_clients()


# -------------------------------------------------------------------- writing


def test_vscode_gets_its_own_schema(config, home):
    main(["install", "vscode", "--write", "--vault", str(config.vault)])

    written = json.loads(_vscode(home).read_text(encoding="utf-8"))
    entry = written["servers"]["bindery"]
    assert entry["type"] == "stdio"          # VS Code requires the transport
    assert entry["args"][-1] == "serve"
    assert entry["env"]["BINDERY_VAULT"] == str(config.vault)
    assert "mcpServers" not in written


def test_vscode_keeps_other_servers_and_inputs(config, home):
    _vscode(home).write_text(
        json.dumps({
            "servers": {"context7": {"type": "stdio", "command": "npx"}},
            "inputs": [{"id": "KEY", "type": "promptString"}],
        }),
        encoding="utf-8",
    )
    main(["install", "vscode", "--write", "--vault", str(config.vault)])

    written = json.loads(_vscode(home).read_text(encoding="utf-8"))
    assert set(written["servers"]) == {"context7", "bindery"}
    assert written["inputs"][0]["id"] == "KEY"


def test_cursor_uses_the_claude_shaped_schema(config, home):
    main(["install", "cursor", "--write", "--vault", str(config.vault)])

    written = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "bindery" in written["mcpServers"]
    assert "type" not in written["mcpServers"]["bindery"]


def test_cursor_keeps_other_servers(config, home):
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"Figma": {"url": "https://mcp.figma.com/mcp"}}}),
        encoding="utf-8",
    )
    main(["install", "cursor", "--write", "--vault", str(config.vault)])

    written = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert set(written["mcpServers"]) == {"Figma", "bindery"}


def test_antigravity_merges_into_its_profile_even_if_the_file_is_empty(config, home):
    path = home / ".gemini" / "config" / "mcp_config.json"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    main(["install", "antigravity", "--write", "--vault", str(config.vault)])

    written = json.loads(path.read_text(encoding="utf-8"))
    entry = written["mcpServers"]["bindery"]
    assert entry["command"]
    assert entry["args"][-1] == "serve"
    assert entry["env"]["BINDERY_VAULT"] == str(config.vault)
    assert (path.with_suffix(".json.bak")).read_text(encoding="utf-8") == ""


def test_antigravity_preserves_other_servers(config, home):
    path = home / ".gemini" / "config" / "mcp_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"context7": {"command": "npx", "args": ["ctx"]}}}),
        encoding="utf-8",
    )

    main(["install", "antigravity", "--write", "--vault", str(config.vault)])

    written = json.loads(path.read_text(encoding="utf-8"))
    assert set(written["mcpServers"]) == {"context7", "bindery"}


def test_antigravity_local_scope_uses_workspace_profile(config, home, monkeypatch):
    project = home / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    main(["install", "antigravity", "--write", "--local", "--vault", str(config.vault)])

    path = project / ".agents" / "mcp_config.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert "bindery" in written["mcpServers"]


def test_a_codex_account_home_is_written_as_toml(config, home):
    main(["install", "codex:codexb", "--write", "--vault", str(config.vault)])

    body = (home / ".codex-homes" / "codexb" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.bindery]" in body
    assert str(config.vault) in body


def test_installing_everything_reaches_every_client(config, home):
    main(["install", "--write", "--vault", str(config.vault)])

    assert "bindery" in json.loads((home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "bindery" in json.loads(_vscode(home).read_text(encoding="utf-8"))["servers"]
    assert "bindery" in json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "bindery" in json.loads(
        (home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )["mcpServers"]
    for account in ("codexb", "codexc"):
        body = (home / ".codex-homes" / account / "config.toml").read_text(encoding="utf-8")
        assert "[mcp_servers.bindery]" in body
    assert "[mcp_servers.bindery]" in (home / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_reinstalling_updates_rather_than_duplicating(config, home):
    main(["install", "--write", "--vault", str(config.vault)])
    main(["install", "--write", "--vault", str(config.vault)])

    written = json.loads(_vscode(home).read_text(encoding="utf-8"))
    assert list(written["servers"]) == ["bindery"]
    body = (home / ".codex-homes" / "codexb" / "config.toml").read_text(encoding="utf-8")
    assert body.count("[mcp_servers.bindery]") == 1


def test_an_unknown_client_is_refused(config, home, capsys):
    assert main(["install", "nonesuch", "--write", "--vault", str(config.vault)]) == 2
    assert "Unknown client" in capsys.readouterr().err


def test_the_index_boundary_reaches_every_client(config, home):
    main(["install", "--write", "--vault", str(config.vault), "--include", "work"])

    vscode = json.loads(_vscode(home).read_text(encoding="utf-8"))
    assert vscode["servers"]["bindery"]["env"]["BINDERY_INCLUDE"] == "work"
    body = (home / ".codex-homes" / "codexb" / "config.toml").read_text(encoding="utf-8")
    assert 'BINDERY_INCLUDE = "work"' in body


@pytest.mark.parametrize("client", ["claude", "codex", "antigravity"])
def test_reinstall_inherits_each_boundary_until_its_cli_flag_is_set(config, home, client, monkeypatch):
    spec = _client_targets(config)[client]
    path = spec["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["--include", "legacy", "serve", "--include=work", "--include", "shared"]
    env = {"BINDERY_INCLUDE": "ignored", "BINDERY_EXCLUDE": "private, drafts"}
    if client == "codex":
        path.write_text(
            '[mcp_servers.bindery]\ncommand = "old-bindery"\n'
            f'args = {json.dumps(args)}\n[mcp_servers.bindery.env]\n'
            'BINDERY_INCLUDE = "ignored"\nBINDERY_EXCLUDE = "private, drafts"\n',
            encoding="utf-8",
        )
    else:
        path.write_text(json.dumps({"mcpServers": {"bindery": {
            "command": "old-bindery", "args": args, "env": env,
        }}}), encoding="utf-8")

    def assert_boundaries(includes, excludes):
        content = path.read_text(encoding="utf-8")
        if client == "codex":
            entry = tomllib.loads(content)["mcp_servers"]["bindery"]
        else:
            entry = json.loads(content)["mcpServers"]["bindery"]
        assert entry["env"] == {
            "BINDERY_VAULT": str(config.vault),
            "BINDERY_INCLUDE": ",".join(includes),
            "BINDERY_EXCLUDE": ",".join(excludes),
        }
        assert entry["args"][-2 * (len(includes) + len(excludes)) - 1:] == [
            *[arg for prefix in includes for arg in ("--include", prefix)],
            *[arg for prefix in excludes for arg in ("--exclude", prefix)],
            "serve",
        ]

    monkeypatch.setenv("BINDERY_INCLUDE", "other")
    monkeypatch.setenv("BINDERY_EXCLUDE", "other")
    install = ["install", client, "--write", "--vault", str(config.vault)]
    monkeypatch.setenv("BINDERY_STATE_DIR", str(config.state_dir))
    assert main(install) == 0
    assert_boundaries(["work", "shared"], ["private", "drafts"])
    assert main(["--include", "new", *install]) == 0
    assert_boundaries(["new"], ["private", "drafts"])
    assert main([*install, "--exclude", "new-private"]) == 0
    assert_boundaries(["new"], ["new-private"])


# ---------------------------------------------------- installing selectively


def test_an_agent_that_is_not_installed_is_left_alone(config, tmp_path, monkeypatch):
    """Creating a config for an app the user does not have is litter."""
    bare = tmp_path / "bare"
    (bare / ".claude").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: bare)

    main(["install", "--write", "--vault", str(config.vault)])

    assert (bare / ".claude.json").exists()
    assert not (bare / ".cursor" / "mcp.json").exists()


def test_naming_a_client_installs_it_anyway(config, tmp_path, monkeypatch):
    """For setting Bindery up before the agent."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: bare)

    main(["install", "cursor", "--write", "--vault", str(config.vault)])
    assert (bare / ".cursor" / "mcp.json").exists()


def test_the_health_check_writes_inside_the_index_boundary(config, home, capsys):
    """Otherwise a correctly-working allowlist reports itself as a failure."""
    from bindery.cli import main

    (config.vault / "work").mkdir()
    (config.vault / "work" / "a.md").write_text("# A\n\n本文。\n", encoding="utf-8")

    main(["setup", "--write", "--vault", str(config.vault),
          "--include", "work", "--no-semantic", "--state-dir", str(config.state_dir)])

    out = capsys.readouterr().out
    assert "write and retrieve   ok" in out
    assert "Bindery is ready" in out


def test_setup_says_when_the_command_is_not_on_your_path(config, home, monkeypatch, capsys):
    """Every instruction in the README is `bindery ...`; silence here is cruel."""
    import bindery.cli as cli

    installed = home / "toolbin"
    installed.mkdir()
    (installed / "python").touch()
    (installed / "bindery").touch()
    monkeypatch.setattr(cli.sys, "executable", str(installed / "python"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    (config.vault / "a.md").write_text("# A\n\n本文。\n", encoding="utf-8")
    main(["setup", "--write", "--vault", str(config.vault),
          "--no-semantic", "--state-dir", str(config.state_dir)])

    out = capsys.readouterr().out
    assert "not on your PATH" in out
    assert str(installed) in out
    assert "Agents are unaffected" in out
