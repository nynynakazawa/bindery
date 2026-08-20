import json

from bindery.config import Config
from bindery.server import MemoryServer


def _call(server, name, args=None):
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    })
    result = response["result"]
    return result["content"][0]["text"], result.get("isError", False)


def test_handshake_reports_protocol_and_identity(config):
    server = MemoryServer(config)
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "bindery"
    assert "tools" in result["capabilities"]


def test_notifications_get_no_reply(config):
    """Replying to a notification is a protocol violation."""
    server = MemoryServer(config)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tool_surface_stays_small(config):
    """Every tool schema costs context tokens on every session, so the surface
    is capped deliberately. Raising this number is a design decision."""
    server = MemoryServer(config)
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert len(tools) == 8
    for tool in tools:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"


def test_unknown_tool_is_a_jsonrpc_error(config):
    server = MemoryServer(config)
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "does_not_exist", "arguments": {}},
    })
    assert response["error"]["code"] == -32601


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
