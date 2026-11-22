import json
from contextlib import closing
from dataclasses import replace

import pytest

from bindery import sync
from bindery.cli import main
from bindery.dashboard import MAX_NOTE_BYTES, payload
from bindery.indexer import reindex
from bindery.store import Store


def invoke(config, capsys, *args, explicit_vault=True):
    common = ["--state-dir", str(config.state_dir), "--project", ""]
    if explicit_vault:
        common += ["--vault", str(config.vault)]
    assert main([*common, *args]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def write_note(vault, rel, text):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


DB_CASES = [
    (["search", "--json", "anything"], {"results": []}),
    (["notes", "--json"], {
        "notes": [], "totals": {"notes": 0, "chunks": 0, "vectors": 0, "links": 0, "projects": {}},
    }),
    (["ranking", "--json"], {"most_used": [], "most_shown": [], "top_queries": []}),
]


@pytest.mark.parametrize(("args", "empty"), DB_CASES)
def test_dashboard_missing_index(config, capsys, args, empty):
    write_note(config.vault, "note.md", "# On disk")
    write_note(config.vault, "journal/2026-11-22.md", "## 10:30\nOn disk")

    result = invoke(config, capsys, *args)
    assert "未索引" in result.pop("error")
    assert result == empty
    assert not config.state_dir.exists()


def test_read_and_recent_work_before_indexing(config, capsys):
    note = write_note(config.vault, "note.md", "# On disk")
    write_note(config.vault, "journal/2026-11-22.md", "## 10:30\nOn disk")

    assert invoke(config, capsys, "read", "--json", "note.md") == {
        "path": "note.md", "title": "On disk", "frontmatter": {}, "body": "# On disk",
        "mtime": note.stat().st_mtime, "error": None,
    }
    assert invoke(config, capsys, "recent", "--json") == {
        "entries": [{
            "path": "journal/2026-11-22.md", "heading": "10:30", "date": "2026-11-22", "tags": [],
        }],
        "error": None,
    }
    assert not config.state_dir.exists()


@pytest.mark.parametrize(("args", "empty"), [
    *DB_CASES, (["read", "--json", "note.md"], None), (["recent", "--json"], {"entries": []}),
])
def test_dashboard_uses_registered_vault(config, tmp_path, monkeypatch, capsys, args, empty):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("BINDERY_VAULT", raising=False)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"bindery": {"args": ["--vault", str(config.vault), "serve"]}}}),
        encoding="utf-8",
    )
    write_note(config.vault, "note.md", "# Registered")
    Store(config.db_path).close()

    result = invoke(config, capsys, *args, explicit_vault=False)
    assert result.pop("error") is None
    assert result == (empty if args[0] != "read" else {
        "path": "note.md", "title": "Registered", "frontmatter": {}, "body": "# Registered",
        "mtime": (config.vault / "note.md").stat().st_mtime,
    })


def test_notes_counts_and_ranking(config, capsys):
    first = write_note(config.vault, "alpha/a.md", "---\ntitle: 認証\ntags: [api, 設計]\n---\n[[B]]\n")
    second = write_note(config.vault, "b.md", "# B\n\nCommon note")
    with closing(Store(config.db_path)) as store:
        reindex(config, store)
        store.record_use(["alpha/a.md"])
        store.record_use(["alpha/a.md"])
        store.record_impressions(["alpha/a.md", "b.md"])
        store.record_impressions(["b.md", "gone.md"])
        store.record_query("abc", 0)
        store.record_query("abc", 3)
        store.record_query("xyz", 2)
        store.conn.execute("INSERT INTO vectors VALUES (?, ?, ?)", (
            store.chunk_ids_for("alpha/a.md")[0], 1, b"\x00\x00\x80\x3f",
        ))
        used = store.usage_map()["alpha/a.md"][1]

    assert invoke(config, capsys, "notes", "--json") == {
        "notes": [
            {"path": "alpha/a.md", "title": "認証", "project": "alpha", "tags": ["api", "設計"],
             "tier": "durable", "mtime": first.stat().st_mtime, "chunks": 1, "uses": 2,
             "impressions": 1, "last_used": used},
            {"path": "b.md", "title": "B", "project": "", "tags": [],
             "tier": "durable", "mtime": second.stat().st_mtime, "chunks": 1, "uses": 0,
             "impressions": 2, "last_used": 0.0},
        ],
        "totals": {"notes": 2, "chunks": 2, "vectors": 1, "links": 1, "projects": {"alpha": 1, "": 1}},
        "error": None,
    }
    assert invoke(config, capsys, "ranking", "--json") == {
        "most_used": [{"path": "alpha/a.md", "title": "認証", "uses": 2}],
        "most_shown": [
            {"path": "b.md", "title": "B", "impressions": 2},
            {"path": "alpha/a.md", "title": "認証", "impressions": 1},
        ],
        "top_queries": [
            {"text": "abc", "count": 2, "avg_hits": 1.5},
            {"text": "xyz", "count": 1, "avg_hits": 2.0},
        ],
        "error": None,
    }
    limited = invoke(config, capsys, "ranking", "--json", "--limit", "1")
    assert [len(limited[key]) for key in ("most_used", "most_shown", "top_queries")] == [1, 1, 1]


def test_dashboard_redacts_index_metadata_and_merges_redacted_project_counts(config, capsys):
    with closing(Store(config.db_path)) as store:
        for label in ("fixture-one", "fixture-two"):
            path = f"{label}.md"
            secret = f"token: {label}"
            store.upsert_note(path=path, title=secret, project=secret, tags=[secret, "work"],
                              mtime=0, digest=label, chunks=[], links=[])
            store.record_use([path])
            store.record_impressions([path])
            store.record_query(secret, 1)

    masked = "token: [redacted]"
    notes = invoke(config, capsys, "notes", "--json")
    assert notes["error"] is None
    assert notes["totals"]["projects"] == {masked: 2}
    assert [(note["title"], note["project"], note["tags"]) for note in notes["notes"]] == [
        (masked, masked, [masked, "work"]), (masked, masked, [masked, "work"]),
    ]
    ranking = invoke(config, capsys, "ranking", "--json")
    assert [row["title"] for row in ranking["most_used"]] == [masked, masked]
    assert [row["title"] for row in ranking["most_shown"]] == [masked, masked]
    assert [row["text"] for row in ranking["top_queries"]] == [masked, masked]


def test_search_uses_existing_index_budget_and_leaves_no_telemetry(config, capsys):
    write_note(config.vault, "alpha/note.md", "# 設計\n\nSupernova design.")
    with closing(Store(config.db_path)) as store:
        reindex(config, store)
        store.record_query("earlier", 4)
        store.record_impressions(["alpha/note.md"])

    # The search must use the existing index even after the Markdown changes.
    write_note(config.vault, "alpha/note.md", "# Revised\n\nDifferent text.")
    before = config.db_path.read_bytes()
    result = invoke(config, capsys, "search", "--json", "supernova", "--project", "beta", "--limit", "1")
    assert result["error"] is None
    [hit] = result["results"]
    assert isinstance(hit["score"], float) and hit["score"] > 0
    assert {k: v for k, v in hit.items() if k != "score"} == {
        "path": "alpha/note.md", "heading": "設計", "snippet": "Supernova design.", "match": "keyword",
    }
    assert invoke(config, capsys, "search", "--json", "different") == {"results": [], "error": None}
    assert invoke(config, capsys, "search", "--json", "supernova", "--max-tokens", "1") == {
        "results": [], "error": None,
    }
    assert config.db_path.read_bytes() == before
    with closing(Store(config.db_path, read_only=True)) as store:
        assert [tuple(r) for r in store.conn.execute("SELECT text, hits FROM queries")] == [("earlier", 4)]
        assert [tuple(r) for r in store.conn.execute("SELECT path, uses, impressions FROM usage")] == [
            ("alpha/note.md", 0, 1),
        ]


def test_search_uses_vectors_without_creating_ann_table(config, monkeypatch, capsys):
    class Backend:
        def encode(self, texts):
            return [[1.0, 0.0] for _ in texts]

    def load_backend(*, local_files_only):
        assert local_files_only is True
        return Backend()

    monkeypatch.setattr("bindery.embed.load_backend", load_backend)
    monkeypatch.setenv("BINDERY_SEMANTIC", "1")
    write_note(config.vault, "note.md", "# Note\n\nExisting passage")
    with closing(Store(config.db_path)) as store:
        reindex(config, store)
        store.conn.execute("INSERT INTO vectors VALUES (?, ?, ?)", (
            store.chunk_ids_for("note.md")[0], 2, b"\x00\x00\x80\x3f\x00\x00\x00\x00",
        ))

    result = invoke(config, capsys, "search", "--json", "unmatched")
    assert result["error"] is None
    assert [hit["match"] for hit in result["results"]] == ["semantic"]
    with closing(Store(config.db_path, read_only=True)) as store:
        assert store.get_meta("vector_dim") == ""
        assert store.conn.execute("SELECT name FROM sqlite_master WHERE name='vec_chunks'").fetchone() is None


def test_dashboard_does_not_migrate_existing_index(config, capsys):
    with closing(Store(config.db_path)) as store:
        store.set_meta("schema_version", "1")
    before = config.db_path.read_bytes()
    for args, empty in DB_CASES:
        result = invoke(config, capsys, *args)
        assert result.pop("error")
        assert result == empty
    assert config.db_path.read_bytes() == before


def test_read_markdown_and_redact_secrets(config, capsys):
    Store(config.db_path).close()
    path = write_note(config.vault, "work/設計.md", (
        "---\ntitle: 詳細\ntags: [sk-fixture123456789012345, work, 設計]\napi_key: sample-secret-value\n---\n\n# 本文\n"
    ))
    assert invoke(config, capsys, "read", "--json", "work/設計.md") == {
        "path": "work/設計.md", "title": "詳細",
        "frontmatter": {"title": "詳細", "tags": ["[redacted]", "work", "設計"], "api_key": "[redacted]"},
        "body": "# 本文", "mtime": path.stat().st_mtime, "error": None,
    }
    (config.vault / "alias.md").symlink_to(path)
    assert invoke(config, capsys, "read", "--json", "alias.md")["path"] == "alias.md"


def test_read_refuses_unsafe_paths_and_invalid_markdown(config, tmp_path, capsys):
    Store(config.db_path).close()
    note = write_note(config.vault, "good.md", "# OK")
    outside = write_note(tmp_path, "outside.md", "# Outside")
    wrong_type = write_note(config.vault, "plain.txt", "Text")
    (config.vault / "escape.md").symlink_to(outside)
    (config.vault / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    (config.vault / "wrong-type.md").symlink_to(wrong_type)
    (config.vault / "invalid.md").write_bytes(b"\xff")
    with (config.vault / "huge.md").open("wb") as handle:
        handle.truncate(2 * 1024 * 1024 + 1)
    for invalid in (
        "../outside.md", "work/../good.md", str(note), "escape.md", "escape-dir/outside.md",
        "plain.txt", "wrong-type.md", "invalid.md", "huge.md",
    ):
        result = invoke(config, capsys, "read", "--json", invalid)
        assert result.pop("error"), invalid
        assert result == {"path": "", "title": "", "frontmatter": {}, "body": "", "mtime": 0.0}


@pytest.mark.parametrize("symlink_is_parent", [False, True])
def test_read_and_recent_accept_symlinked_vault_roots(config, tmp_path, symlink_is_parent):
    write_note(config.vault, "note.md", "# In the vault")
    write_note(config.vault, "journal/2026-11-22.md", "## Journal entry")
    link = tmp_path / "linked"
    link.symlink_to(config.vault.parent if symlink_is_parent else config.vault, target_is_directory=True)
    linked_config = replace(config, vault=link / config.vault.name if symlink_is_parent else link)

    assert payload(linked_config, "read", value="note.md")["title"] == "In the vault"
    assert [entry["heading"] for entry in payload(linked_config, "recent")["entries"]] == ["Journal entry"]
    (config.vault / "escape.md").symlink_to(write_note(tmp_path, "outside.md", "# Outside"))
    with pytest.raises(ValueError, match="Vault の外"):
        payload(linked_config, "read", value="escape.md")


def test_read_and_recent_accept_mixed_case_markdown_extensions(config, capsys):
    note = write_note(config.vault, "note.MD", "# Uppercase")
    (config.vault / "alias.md").symlink_to(note)
    write_note(config.vault, "journal/2026-11-22.Md", "## Journal entry")

    for path in ("note.MD", "alias.md"):
        assert invoke(config, capsys, "read", "--json", path)["title"] == "Uppercase"
    assert [entry["path"] for entry in invoke(config, capsys, "recent", "--json")["entries"]] == [
        "journal/2026-11-22.Md",
    ]


def test_recent_skips_journal_files_over_size_limit(config, capsys):
    oversized = write_note(config.vault, "journal/2026-11-23.md", "## Oversized\n")
    with oversized.open("r+b") as handle:
        handle.truncate(MAX_NOTE_BYTES + 1)
    write_note(config.vault, "journal/2026-11-22.md", "## Fits")

    result = invoke(config, capsys, "recent", "--json", "--limit", "1")
    assert result["error"] is None
    assert [entry["heading"] for entry in result["entries"]] == ["Fits"]


def test_recent_lists_dated_journal_headings(config, capsys):
    Store(config.db_path).close()
    write_note(config.vault, "journal/2026-11-21.md", "# Journal\n\n## 昨日の記録\n")
    write_note(config.vault, "journal/proj/2026-11-22.mac.md", (
        "---\ntags: [work, review]\n---\n# Journal\n\n## 10:30\n\n"
        "```markdown\n## Example\n```\n\n## 12:00\n\n### Subheading\n"
    ))
    write_note(config.vault, "journal/sessions/2026-11-23.md", "## Session")
    write_note(config.vault, "journal/episodes/project/2026-11-23-codex-id.md", "## Episode")
    write_note(config.vault, "2026-11-23.md", "## Not a journal")

    result = invoke(config, capsys, "recent", "--json")
    assert result == {"entries": [
        {"path": "journal/proj/2026-11-22.mac.md", "heading": "12:00", "date": "2026-11-22",
         "tags": ["work", "review"]},
        {"path": "journal/proj/2026-11-22.mac.md", "heading": "10:30", "date": "2026-11-22",
         "tags": ["work", "review"]},
        {"path": "journal/2026-11-21.md", "heading": "昨日の記録", "date": "2026-11-21", "tags": []},
    ], "error": None}
    assert invoke(config, capsys, "recent", "--json", "--limit", "1")["entries"] == result["entries"][:1]


def test_sync_status_lists_held_and_current_conflict_paths(config, tmp_path, capsys):
    assert not config.state_dir.exists()
    empty = invoke(config, capsys, "sync", "status", "--json")
    assert empty["held_paths"] == empty["conflict_paths"] == empty["secret_paths"] == []
    assert not config.state_dir.exists()

    paths = sync.paths_for(config)
    sync._save_state(paths, {
        "held_secrets": ["work/保留.md", "../unsafe.md"],
        "conflicts": 7,
        "secret_paths": ["work/secret.md", "work/missing.md", "../unsafe.md", "secret-link.md"],
    })
    conflict = sync._conflict_path("work/note.md", "a" * 64)
    write_note(config.vault, "work/secret.md", "Test note")
    write_note(config.vault, conflict, "Conflict content")
    write_note(config.vault, "work/not-a.conflict-draft.md", "Not a generated copy")
    outside = write_note(tmp_path, "outside.md", "Outside")
    (config.vault / "link.conflict-0123456789ab.md").symlink_to(outside)
    (config.vault / "secret-link.md").symlink_to(outside)

    result = invoke(config, capsys, "sync", "status", "--json")
    assert result == {
        **sync.sync_status(config),
        "held_paths": ["work/保留.md"],
        "conflict_paths": [conflict],
        "secret_paths": ["work/secret.md"],
    }
    assert result["held_secrets"] == 2 and result["conflicts"] == 7 and result["secret_count"] == 1
    assert not config.db_path.exists()


def test_sync_run_json_includes_held_and_conflict_paths_when_blocked(config, capsys):
    paths = sync.paths_for(config)
    sync._save_state(paths, {"held_secrets": ["work/held.env"]})
    conflict = sync._conflict_path("work/note.md", "a" * 64)
    write_note(config.vault, conflict, "Conflict content")

    assert main(["--vault", str(config.vault), "--state-dir", str(config.state_dir), "sync", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == 2
    assert result["status"]["blocked_reason"]
    assert result["status"]["held_paths"] == ["work/held.env"]
    assert result["status"]["conflict_paths"] == [conflict]


def test_connections_are_classified_by_executable_not_prompt(monkeypatch):
    """A prompt that mentions another tool must not change who is connected."""
    import subprocess as sp
    from bindery import cli

    commands = "\n".join([
        "  100     1 Sun Nov 22 22:00:00 2026 agy --mode plan -p ask bindery and claude about codex",
        "  101   100 Sun Nov 22 22:00:01 2026 /tools/bin/python3 /home/u/.local/bin/bindery --vault /v serve",
        "  200     1 Sun Nov 22 22:00:00 2026 /opt/codex/bin/codex exec use the bindery memory",
        "  201   200 Sun Nov 22 22:00:01 2026 /tools/bin/python3 /home/u/.local/bin/bindery --vault /v serve",
        "  300     1 Sun Nov 22 22:00:00 2026 node /usr/local/lib/node_modules/@openai/codex/bin/codex.js",
        "  301   300 Sun Nov 22 22:00:01 2026 /tools/bin/python3 /home/u/.local/bin/bindery serve",
        "  400     1 Sun Nov 22 22:00:00 2026 /Users/u/Library/Application Support/Claude/claude.app/Contents/MacOS/claude --print codex",
        "  401   400 Sun Nov 22 22:00:01 2026 /tools/bin/python3 /home/u/.local/bin/bindery serve",
    ])
    comms = "\n".join([
        "100 agy", "101 /tools/bin/python3", "200 /opt/codex/bin/codex", "201 /tools/bin/python3",
        "300 node", "301 /tools/bin/python3",
        "400 /Users/u/Library/Application Support/Claude/claude.app/Contents/MacOS/claude", "401 /tools/bin/python3",
    ])

    def fake_run(args, **kwargs):
        out = comms if "pid=,comm=" in args else commands
        return sp.CompletedProcess(args, 0, out + "\n", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    found = {row["pid"]: row["client"] for row in cli._connections()}
    assert found == {101: "antigravity", 201: "codex", 301: "codex", 401: "claude"}
