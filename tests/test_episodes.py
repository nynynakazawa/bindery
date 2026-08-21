"""Capturing what agents forgot to write down.

The value of this subsystem is entirely in what it *leaves out*: a transcript
kept whole is not memory, it is a liability - slow to search, full of secrets,
and heavy enough to crowd out the notes somebody wrote on purpose.
"""

from __future__ import annotations

import json
import time

import pytest

from bindery.adapters import Session, SessionRef, Turn
from bindery.adapters.claude import ClaudeAdapter
from bindery.adapters.codex import CodexAdapter
from bindery.config import Config
from bindery.episodes import (
    ImportReport,
    import_new,
    load_state,
    reduce_session,
    set_baseline,
    worth_recording,
)
from bindery.server import MemoryServer
from bindery.store import Store

OLD = time.time() - 3600  # comfortably past the quiet threshold


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    (root / ".claude" / "projects" / "proj").mkdir(parents=True)
    (root / ".codex" / "sessions" / "2026" / "08" / "20").mkdir(parents=True)
    return root


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("BINDERY_EPISODES", "1")
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config.resolve(
        vault=vault, state_dir=tmp_path / "state", semantic=False, project="alpha"
    )


def _claude_session(home, name, records, mtime=OLD):
    path = home / ".claude" / "projects" / "proj" / f"{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    import os

    os.utime(path, (mtime, mtime))
    return path


def _codex_session(home, name, records, mtime=OLD):
    path = home / ".codex" / "sessions" / "2026" / "08" / "20" / f"rollout-{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    import os

    os.utime(path, (mtime, mtime))
    return path


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}, "cwd": "/w"}


def _assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _text(t):
    return {"type": "text", "text": t}


def _real_session(turns=None):
    ref = SessionRef(path=None, session_id="s1", client="claude", modified=OLD)
    return Session(ref=ref, turns=turns or [])


# ----------------------------------------------------------- the reducer


def test_hidden_reasoning_never_reaches_the_episode(home, config):
    _claude_session(home, "a", [
        _user("認証まわりを直したい。方式の候補を比較して決めてほしい。"),
        _assistant(
            {"type": "thinking", "thinking": "SECRETREASONING internal chain"},
            _text("Firebase Auth を使います。"),
        ),
        _user("それで進めて。"),
    ])
    adapter = ClaudeAdapter(home)
    body = reduce_session(adapter.normalize(adapter.discover()[0]))

    assert "Firebase Auth" in body
    assert "SECRETREASONING" not in body


def test_successful_output_is_dropped_and_failures_are_kept():
    session = _real_session([
        Turn("command", "pytest -q"),
        Turn("result", "\n".join(f"ok line {i}" for i in range(500))),
        Turn("command", "pytest -q"),
        Turn("result", "E   AssertionError: expected 3 got 4\n1 failed, 12 passed"),
    ])
    body = reduce_session(session)

    assert "ok line 250" not in body
    assert "AssertionError" in body
    assert "1 failed" in body


def test_a_pasted_file_is_clipped_not_stored():
    session = _real_session([
        Turn("user", "これを直して:\n" + "x" * 50000),
        Turn("agent", "直しました。"),
        Turn("user", "ありがとう"),
    ])
    body = reduce_session(session)

    assert len(body) < 5000
    assert "more characters" in body


def test_repeated_identical_commands_collapse():
    session = _real_session(
        [Turn("user", "テストを通るまで直して。何度か試して。"), Turn("agent", "はい")]
        + [Turn("command", "pytest -q") for _ in range(40)]
        + [Turn("user", "ありがとう")]
    )
    body = reduce_session(session)
    assert body.count("pytest -q") == 1


def test_a_giant_session_is_bounded():
    session = _real_session(
        [Turn("user", "長い作業をお願いします。詳細は以下の通りです。" * 5)]
        + [Turn("agent", f"手順 {i} を実行しました。" * 20) for i in range(400)]
        + [Turn("user", "完了確認")]
    )
    body = reduce_session(session)

    assert len(body) <= 16200
    assert "session continues" in body


def test_dependency_noise_is_not_kept():
    session = _real_session([
        Turn("user", "ビルドが壊れているので直してください。原因を調べて。"),
        Turn("agent", "確認します"),
        Turn("result", "Error: node_modules/foo/bar.js:1 Unexpected token"),
        Turn("user", "ありがとう"),
    ])
    assert "node_modules" not in reduce_session(session)


def test_secrets_never_reach_an_episode():
    session = _real_session([
        Turn("user", "環境変数を設定したい。手順を教えてください。"),
        Turn("command", "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456"),
        Turn("agent", "設定しました。"),
        Turn("user", "確認して"),
    ])
    body = reduce_session(session)

    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in body
    assert "[redacted]" in body


def test_a_private_block_is_removed():
    session = _real_session([
        Turn("user", "これは記録しないで <private>個人的な事情がある</private> あとは進めて"),
        Turn("agent", "了解しました。"),
        Turn("user", "ありがとう"),
    ])
    body = reduce_session(session)
    assert "個人的な事情" not in body


def test_binderys_own_traffic_is_not_recorded_as_memory(home, config):
    """Otherwise the memory becomes mostly a record of itself."""
    _claude_session(home, "a", [
        _user("過去の認証まわりの判断を探して、まとめてください。"),
        _assistant({"type": "tool_use", "name": "mcp__bindery__memory_search",
                    "input": {"query": "認証"}}),
        _assistant(_text("見つかりました。")),
        _user("ありがとう"),
    ])
    adapter = ClaudeAdapter(home)
    body = reduce_session(adapter.normalize(adapter.discover()[0]))
    assert "memory_search" not in body


def test_a_trivial_session_is_not_worth_a_file():
    assert not worth_recording(_real_session([Turn("user", "hi"), Turn("agent", "hello")]))


# ------------------------------------------------------------- adapters


def test_codex_transcripts_normalize_to_the_same_shape(home):
    _codex_session(home, "abc", [
        {"type": "session_meta", "payload": {"id": "abc", "cwd": "/w"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "DBを選びたい"}},
        {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "HIDDEN"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "SQLite にします"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                              "arguments": json.dumps({"cmd": "sqlite3 --version"})}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
    ])
    adapter = CodexAdapter(home)
    session = adapter.normalize(adapter.discover()[0])
    kinds = [t.kind for t in session.turns]

    assert "user" in kinds and "agent" in kinds and "command" in kinds
    body = reduce_session(session)
    assert "HIDDEN" not in body
    assert "sqlite3 --version" in body
    assert session.project_hint == "/w"


def test_per_account_codex_homes_are_discovered(home):
    account = home / ".codex-homes" / "codexb" / "sessions"
    account.mkdir(parents=True)
    (account / "rollout-xyz.jsonl").write_text("{}\n", encoding="utf-8")

    ids = {ref.session_id for ref in CodexAdapter(home).discover()}
    assert "xyz" in ids


def test_a_live_session_is_left_alone(home):
    _claude_session(home, "live", [_user("まだ作業中")], mtime=time.time())
    adapter = ClaudeAdapter(home)
    assert not adapter.is_complete(adapter.discover()[0])


# -------------------------------------------------------------- importing


def test_baseline_stops_years_of_history_being_ingested(home, config):
    _claude_session(home, "old-one", [_user("昔の会話"), _assistant(_text("はい")), _user("ok")])
    set_baseline(config, home)

    store = Store(config.db_path)
    report = import_new(config, store, home=home)

    assert report.imported == 0
    assert not list(config.vault.rglob("*.md"))


def test_a_session_after_the_baseline_is_captured(home, config):
    set_baseline(config, home)
    _claude_session(home, "new-one", [
        _user("SQLite の WAL について調べて、採用可否を判断したい。"),
        _assistant(_text("WAL を有効にします。")),
        _user("進めて"),
    ])

    store = Store(config.db_path)
    report = import_new(config, store, home=home)

    assert report.imported == 1
    episodes = list((config.vault / "journal" / "episodes").rglob("*.md"))
    assert len(episodes) == 1
    assert "WAL" in episodes[0].read_text(encoding="utf-8")


def test_importing_twice_does_not_duplicate(home, config):
    set_baseline(config, home)
    _claude_session(home, "new-one", [
        _user("SQLite の WAL について調べて、採用可否を判断したい。"),
        _assistant(_text("WAL を有効にします。")),
        _user("進めて"),
    ])
    store = Store(config.db_path)

    import_new(config, store, home=home)
    second = import_new(config, store, home=home)

    assert second.imported == 0
    assert len(list((config.vault / "journal" / "episodes").rglob("*.md"))) == 1


def test_a_corrupt_transcript_is_quarantined_not_retried(home, config, monkeypatch):
    set_baseline(config, home)
    path = home / ".claude" / "projects" / "proj" / "broken.jsonl"
    path.write_text("not json at all\n", encoding="utf-8")
    import os

    os.utime(path, (OLD, OLD))

    def explode(self, ref):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(ClaudeAdapter, "normalize", explode)
    store = Store(config.db_path)
    report = import_new(config, store, home=home)

    assert report.failed == 1
    assert "claude:broken" in load_state(config)["quarantined"]


def test_an_import_failure_never_stops_the_server(home, config, monkeypatch):
    """Capturing the past must not stop the agent working now."""
    def explode(*args, **kwargs):
        raise RuntimeError("import exploded")

    monkeypatch.setattr("bindery.episodes.import_new", explode)
    server = MemoryServer(config)
    assert "Wrote" in server.call_tool("memory_write", {"path": "a.md", "content": "本文"})


def test_episodes_are_searchable_but_rank_below_written_notes(home, config, monkeypatch):
    """A note someone wrote on purpose outranks a transcript of them working it out."""
    set_baseline(config, home)
    (config.vault / "adr").mkdir()
    (config.vault / "adr" / "db.md").write_text(
        "---\nproject: alpha\n---\n\n# DB\n\nSQLite の WAL を有効にする。\n", encoding="utf-8"
    )
    _claude_session(home, "new-one", [
        _user("WAL まわりでハマったので、原因と回避策を残しておきたい。"),
        _assistant(_text("WAL の切り替えが busy_timeout より先に走ると落ちます。")),
        _user("進めて"),
    ])
    store = Store(config.db_path)
    assert import_new(config, store, home=home).imported == 1
    store.close()

    # The server would import from the real home, which a test must not touch.
    monkeypatch.setenv("BINDERY_EPISODES", "0")
    server = MemoryServer(config)
    text = server.call_tool("memory_search", {"query": "WAL", "scope": "all"})

    assert "adr/db.md" in text
    assert "journal/episodes" in text
    assert text.index("adr/db.md") < text.index("journal/episodes")


def test_import_is_bounded_per_run(home, config):
    set_baseline(config, home)
    for i in range(8):
        _claude_session(home, f"s{i}", [
            _user(f"セッション {i} の作業内容について相談したいことがあります。"),
            _assistant(_text("承知しました。")),
            _user("進めて"),
        ])
    store = Store(config.db_path)

    report = import_new(config, store, home=home, limit=3)
    assert report.imported == 3


def test_the_report_says_what_happened():
    assert ImportReport(scanned=4, imported=2).as_dict()["imported"] == 2


def test_the_same_fact_is_not_returned_from_three_tiers_at_once(home, config, monkeypatch):
    """A durable note, the journal entry behind it, and the episode under that.

    All three match; returning all three spends the budget saying one thing.
    """
    same = "デプロイは同一成果物を環境間で昇格させる。ビルドは一度だけ行う。"
    (config.vault / "adr").mkdir()
    (config.vault / "adr" / "deploy.md").write_text(
        f"---\nproject: alpha\n---\n\n# Deploy\n\n{same}\n", encoding="utf-8"
    )
    (config.vault / "journal" / "alpha").mkdir(parents=True)
    (config.vault / "journal" / "alpha" / "2026-08-20.md").write_text(
        f"---\nproject: alpha\n---\n\n# Journal\n\n{same}\n", encoding="utf-8"
    )

    monkeypatch.setenv("BINDERY_EPISODES", "0")
    server = MemoryServer(config)
    text = server.call_tool("memory_search", {"query": "デプロイ 昇格", "scope": "all"})

    assert text.count("デプロイは同一成果物") == 1
    assert "adr/deploy.md" in text


def test_distinct_passages_are_all_returned(home, config, monkeypatch):
    """Suppression must not collapse different answers to the same question."""
    (config.vault / "adr").mkdir()
    (config.vault / "adr" / "a.md").write_text(
        "---\nproject: alpha\n---\n\n# A\n\n認証は Firebase Auth を使う。\n", encoding="utf-8"
    )
    (config.vault / "adr" / "b.md").write_text(
        "---\nproject: alpha\n---\n\n# B\n\n認証トークンの失効は30日とする。\n", encoding="utf-8"
    )

    monkeypatch.setenv("BINDERY_EPISODES", "0")
    server = MemoryServer(config)
    text = server.call_tool("memory_search", {"query": "認証", "scope": "all"})

    assert "Firebase" in text and "30日" in text
