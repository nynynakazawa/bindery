"""The memory tools, called directly.

Protocol behaviour - the handshake, schemas, error shapes - is the SDK's
responsibility now and is covered in test_protocol.py against a real client.
What is left here is what this project actually implements.
"""

import json

from bindery.config import Config
from bindery.server import MemoryServer


def _call(server, name, args=None):
    """Invoke a tool the way the MCP layer does, without the transport."""
    try:
        return server.call_tool(name, args or {}), False
    except Exception as exc:  # mirrors what the SDK reports back to the model
        return f"{type(exc).__name__}: {exc}", True


def test_unknown_tool_is_refused(config):
    server = MemoryServer(config)
    _text, error = _call(server, "does_not_exist", {})
    assert error


def test_write_then_search_round_trip(config):
    server = MemoryServer(config)
    _call(server, "memory_write", {"path": "adr/cache.md", "content": "Redis は使わない。", "title": "キャッシュ方針"})
    text, error = _call(server, "memory_search", {"query": "Redis"})
    assert not error
    assert "Redis は使わない" in text


def test_write_appends_markdown_extension(config):
    server = MemoryServer(config)
    _call(server, "memory_write", {"path": "no-extension", "content": "body"})
    assert (config.vault / "no-extension.md").exists()


def test_path_traversal_is_refused(config, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    server = MemoryServer(config)

    read_text, _ = _call(server, "memory_read", {"path": "../secret.txt"})
    assert "Refused" in read_text
    assert "classified" not in read_text

    _call(server, "memory_write", {"path": "../escaped.md", "content": "nope"})
    assert not (tmp_path / "escaped.md").exists()


def test_read_truncates_at_the_budget(config):
    (config.vault / "long.md").write_text("# L\n\n" + "word " * 5000, encoding="utf-8")
    server = MemoryServer(config)
    text, _ = _call(server, "memory_read", {"path": "long.md", "max_tokens": 50})
    assert "truncated" in text
    assert len(text) < 5000


def test_links_resolve_in_both_directions(config):
    (config.vault / "a.md").write_text("# 認証方式\n\n[[インフラ構成]] を参照。\n", encoding="utf-8")
    (config.vault / "b.md").write_text("# インフラ構成\n\n[[認証方式]] も見る。\n", encoding="utf-8")
    server = MemoryServer(config)
    payload = json.loads(_call(server, "memory_links", {"path": "a.md"})[0])
    assert payload["links_to"] == ["インフラ構成"]
    assert payload["linked_from"] == ["b.md"]


def test_status_is_valid_json_with_the_vault_path(config):
    server = MemoryServer(config)
    payload = json.loads(_call(server, "memory_status")[0])
    assert payload["vault"] == str(config.vault)
    assert "semantic_search" in payload


def test_two_agents_share_one_memory(config, tmp_path):
    """The core promise: what Claude Code writes, Codex can find.

    Two independent server processes are simulated by two MemoryServer
    instances resolved from separate Config objects pointing at the same vault.
    """
    claude_side = MemoryServer(config)
    _call(claude_side, "memory_write", {
        "path": "decisions/queue.md",
        "content": "採用したのは SQS。理由は運用コストが低いため。",
        "title": "キュー選定",
    })

    codex_side = MemoryServer(
        Config.resolve(vault=config.vault, state_dir=config.state_dir, semantic=False)
    )
    text, error = _call(codex_side, "memory_search", {"query": "SQS"})
    assert not error
    assert "SQS" in text

    # And the reverse direction.
    _call(codex_side, "memory_write", {"path": "decisions/db.md", "content": "DB は Postgres。"})
    back, _ = _call(claude_side, "memory_search", {"query": "Postgres"})
    assert "Postgres" in back


def test_edits_made_outside_an_agent_are_picked_up(config):
    """Notes edited directly in Obsidian must appear without manual reindexing."""
    server = MemoryServer(config)
    (config.vault / "manual.md").write_text("# 手動\n\nObsidian で直接書いた内容。\n", encoding="utf-8")
    _call(server, "memory_reindex")
    text, _ = _call(server, "memory_search", {"query": "Obsidian"})
    assert "直接書いた" in text
