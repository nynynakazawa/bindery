"""The MCP surface, exercised through a real client.

These run against the SDK's in-memory transport, which is the same code path a
stdio client takes minus the pipe. That is the point of using the SDK: the
handshake, schema generation, and error shapes are its behaviour to get right,
so what is worth testing here is that the tools are wired onto it correctly -
names, arguments, and the lifecycle that automatic capture depends on.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client, Implementation

from bindery.config import Config
from bindery.server import build_server


@pytest.fixture
def config(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config.resolve(
        vault=vault, state_dir=tmp_path / "state", semantic=False, project="alpha"
    )


def _run(coro):
    return asyncio.run(coro)


def _text(result) -> str:
    return "".join(block.text for block in result.content if hasattr(block, "text"))


# ------------------------------------------------------------------ surface


def test_the_server_identifies_itself(config):
    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return client.server_info, client.protocol_version

    info, protocol = _run(go())
    assert info.name == "bindery"
    assert info.version
    # Negotiated by the SDK rather than pinned in this codebase - the reason
    # for using it. A hard-coded protocol version goes stale silently.
    assert protocol


def test_tool_surface_stays_small(config):
    """Every tool schema costs context tokens on every session, so the surface
    is capped deliberately. Raising this number is a design decision."""

    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = (_run(go())).tools
    assert len(tools) == 8
    assert {t.name for t in tools} == {
        "memory_search", "memory_read", "memory_write", "memory_learn",
        "memory_review", "memory_links", "memory_status", "memory_reindex",
    }
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema["type"] == "object"


def test_arguments_carry_their_own_descriptions(config):
    """The model only sees the schema, so an undocumented argument is unusable."""

    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return await client.list_tools()

    search = next(t for t in _run(go()).tools if t.name == "memory_search")
    properties = search.input_schema["properties"]

    assert set(properties) == {"query", "scope", "limit", "max_tokens"}
    assert properties["scope"]["enum"] == ["project", "global", "all"]
    for name, spec in properties.items():
        assert spec.get("description"), f"{name} has no description"


def test_responses_do_not_carry_a_duplicate_payload(config):
    """A `str` return type otherwise makes the SDK send the answer twice.

    Once as text and once as structuredContent, doubling every response in the
    one project where response size is the entire point.
    """

    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return await client.call_tool("memory_status", {})

    result = _run(go())
    assert _text(result)
    assert not getattr(result, "structured_content", None)


def test_unknown_tool_is_an_error(config):
    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return await client.call_tool("does_not_exist", {})

    assert _run(go()).is_error


def test_a_missing_required_argument_is_rejected(config):
    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return await client.call_tool("memory_search", {})

    assert _run(go()).is_error


# ----------------------------------------------------------------- round trip


def test_write_then_search_over_the_protocol(config):
    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            await client.call_tool(
                "memory_write",
                {"path": "adr/cache.md", "content": "Redis は使わない。", "title": "キャッシュ方針"},
            )
            return await client.call_tool("memory_search", {"query": "Redis"})

    assert "Redis は使わない" in _text(_run(go()))


def test_scope_argument_survives_the_round_trip(config):
    (config.vault / "beta").mkdir()
    (config.vault / "beta" / "auth.md").write_text(
        "---\nproject: beta\n---\n\n# Auth\n\n認証は Clerk を使う。\n", encoding="utf-8"
    )

    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            narrow = await client.call_tool("memory_search", {"query": "認証", "scope": "global"})
            wide = await client.call_tool("memory_search", {"query": "認証", "scope": "all"})
            return _text(narrow), _text(wide)

    narrow, wide = _run(go())
    assert "Clerk" not in narrow
    assert "Clerk" in wide


def test_status_comes_back_as_json(config):
    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            return _text(await client.call_tool("memory_status", {}))

    status = json.loads(_run(go()))
    assert status["vault"] == str(config.vault)
    assert status["project"] == "alpha"


# ------------------------------------------------------------------ lifecycle


def test_disconnect_finalizes_the_session(config):
    """EOF is the only moment the server reliably knows a session ended."""

    async def go():
        mcp, memory = build_server(config)
        async with Client(mcp) as client:
            await client.call_tool("memory_search", {"query": "missing alpha"})
            await client.call_tool("memory_search", {"query": "missing beta"})
        return memory

    _run(go())
    records = list((config.vault / "journal" / "sessions").glob("*.md"))
    assert len(records) == 1
    assert "missing alpha" in records[0].read_text(encoding="utf-8")


def test_the_session_record_names_the_agent(config):
    async def go():
        mcp, _ = build_server(config)
        info = Implementation(name="codex", version="1")
        async with Client(mcp, client_info=info) as client:
            await client.call_tool("memory_search", {"query": "gap one"})
            await client.call_tool("memory_search", {"query": "gap two"})

    _run(go())
    record = next((config.vault / "journal" / "sessions").glob("*.md"))
    assert "codex" in record.read_text(encoding="utf-8")


def test_a_quiet_session_records_nothing(config):
    """A session that only found what it needed taught the system nothing."""

    async def go():
        mcp, _ = build_server(config)
        async with Client(mcp) as client:
            await client.call_tool("memory_status", {})

    _run(go())
    assert not list((config.vault / "journal").rglob("*.md"))


def test_a_terminated_server_still_writes_its_session_record(config, tmp_path):
    """Quitting an agent sends SIGTERM, which is the common way a session ends."""
    import json
    import os
    import signal
    import subprocess
    import sys
    import textwrap
    import time

    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from bindery.config import Config
        from bindery.server import serve

        config = Config.resolve(
            vault=sys.argv[2], state_dir=sys.argv[3], semantic=False, project="alpha"
        )
        serve(config)
        """
    )
    src = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "BINDERY_EPISODES": "0", "BINDERY_SEMANTIC": "0"}
    process = subprocess.Popen(
        [sys.executable, "-c", script, src, str(config.vault), str(config.state_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    )
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "memory_search", "arguments": {"query": "missing alpha"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "memory_search", "arguments": {"query": "missing beta"}}},
        ):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        deadline = time.time() + 20
        seen = 0
        while seen < 3 and time.time() < deadline:
            if process.stdout.readline():
                seen += 1
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
    finally:
        if process.poll() is None:  # pragma: no cover
            process.kill()

    records = list((config.vault / "journal" / "sessions").glob("*.md"))
    assert records, "SIGTERM lost the session record"
    assert "missing alpha" in records[0].read_text(encoding="utf-8")
