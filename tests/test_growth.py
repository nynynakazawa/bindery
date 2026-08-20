import json
import time

from bindery.growth import (
    USAGE_HALF_LIFE_DAYS,
    apply_usage_boost,
    find_duplicates,
    hot_paths,
    knowledge_gaps,
    promotion_candidates,
    stale_notes,
    usage_score,
)
from bindery.search import search
from bindery.server import MemoryServer
from bindery.store import Store

DAY = 86400.0


def _call(server, name, args=None):
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    })["result"]
    return response["content"][0]["text"], response.get("isError", False)


# ------------------------------------------------------------- weighting --


def test_unused_note_has_no_weight():
    assert usage_score("a.md", {}, time.time()) == 0.0


def test_weight_grows_with_use_but_saturates():
    now = time.time()
    one = usage_score("a.md", {"a.md": (1, now, 0)}, now)
    ten = usage_score("a.md", {"a.md": (10, now, 0)}, now)
    thousand = usage_score("a.md", {"a.md": (1000, now, 0)}, now)
    assert one < ten <= thousand <= 1.0
    # A note read a thousand times must not be a thousand times louder.
    assert thousand < one * 6


def test_weight_decays_with_a_thirty_day_half_life():
    now = time.time()
    fresh = usage_score("a.md", {"a.md": (8, now, 0)}, now)
    aged = usage_score("a.md", {"a.md": (8, now - USAGE_HALF_LIFE_DAYS * DAY, 0)}, now)
    assert abs(aged - fresh / 2) < fresh * 0.05


def test_pinned_notes_keep_full_weight_forever():
    now = time.time()
    assert usage_score("a.md", {"a.md": (0, now - 400 * DAY, 1)}, now) == 1.0


def test_boost_can_reorder_ties_but_not_override_content_match():
    now = time.time()
    scored = [(1, 1.00, ["keyword"]), (2, 0.99, ["keyword"]), (3, 0.40, ["keyword"])]
    paths = {1: "cold.md", 2: "warm.md", 3: "hot.md"}
    usage = {"warm.md": (20, now, 0), "hot.md": (20, now, 0)}

    boosted = apply_usage_boost(scored, paths, usage, now=now)
    order = [chunk_id for chunk_id, _, _ in boosted]

    # A near-tie flips in favour of the note that has proven useful...
    assert order[0] == 2
    # ...but a much weaker content match stays last however popular it is.
    assert order[-1] == 3


# ------------------------------------------------------------------ gaps --


def test_unanswered_queries_become_gaps(config, indexed):
    (config.vault / "a.md").write_text("# A\n\nalpha content here\n", encoding="utf-8")
    store = indexed()
    for _ in range(3):
        search(config, store, "deployment runbook")
    search(config, store, "asked once only")

    gaps = knowledge_gaps(store)
    queries = {gap.query: gap.count for gap in gaps}
    assert queries.get("deployment runbook") == 3
    # A one-off question is noise, not a gap.
    assert "asked once only" not in queries


def test_answered_queries_are_not_gaps(config, indexed):
    (config.vault / "a.md").write_text("# A\n\nalpha content here\n", encoding="utf-8")
    store = indexed()
    for _ in range(3):
        search(config, store, "alpha")
    assert [g for g in knowledge_gaps(store) if g.query == "alpha"] == []


def test_retrieval_history_identifies_load_bearing_notes(config, indexed):
    (config.vault / "used.md").write_text("# Used\n\nsignalword here\n", encoding="utf-8")
    (config.vault / "ignored.md").write_text("# Ignored\n\nsomething else\n", encoding="utf-8")
    store = indexed()
    for _ in range(4):
        search(config, store, "signalword")

    hot = hot_paths(store)
    assert hot and hot[0][0] == "used.md"
    assert "ignored.md" not in {path for path, _, _ in hot}


def test_learning_can_be_switched_off(config, indexed):
    (config.vault / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    store = indexed()
    search(config, store, "alpha", learn=False)
    assert store.conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 0


def test_usage_survives_reindexing(config, indexed):
    """Usage is keyed by path, so editing a note must not erase what was learned."""
    from bindery.indexer import reindex

    path = config.vault / "a.md"
    path.write_text("# A\n\nsignalword here\n", encoding="utf-8")
    store = indexed()
    for _ in range(3):
        search(config, store, "signalword")
    before = store.usage_map()["a.md"][0]

    path.write_text("# A\n\nsignalword here, edited\n", encoding="utf-8")
    reindex(config, store)
    assert store.usage_map()["a.md"][0] == before


def test_query_log_is_capped(config, indexed):
    (config.vault / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    store = indexed()
    for index in range(50):
        store.record_query(f"q{index}", 0)
    store.commit()
    removed = store.prune_queries(keep=20)
    assert removed == 30
    assert store.conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 20


# ------------------------------------------------------------ duplicates --


def test_near_duplicate_notes_are_detected(config, indexed):
    shared = ("認証には JWT を採用した。理由は水平スケール時に共有セッションストアを "
              "持たなくて済むためで、失効は15分に設定している。")
    (config.vault / "a.md").write_text(f"# A\n\n{shared}\n", encoding="utf-8")
    (config.vault / "b.md").write_text(f"# B\n\n{shared} なお再検討の余地あり。\n", encoding="utf-8")
    (config.vault / "c.md").write_text("# C\n\n全く関係のない話題。デプロイは ECS で行う。\n", encoding="utf-8")
    store = indexed()

    pairs = find_duplicates(store)
    involved = {tuple(sorted((pair.left, pair.right))) for pair in pairs}
    assert ("a.md", "b.md") in involved
    assert not any("c.md" in pair for pair in involved)


def test_distinct_notes_are_not_flagged(config, indexed):
    (config.vault / "a.md").write_text("# A\n\n認証は JWT を採用した。\n", encoding="utf-8")
    (config.vault / "b.md").write_text("# B\n\nデプロイは ECS Fargate で行う。\n", encoding="utf-8")
    store = indexed()
    assert find_duplicates(store) == []


# ----------------------------------------------------------------- stale --


def test_never_retrieved_old_notes_are_stale(config, indexed):
    import os

    old = config.vault / "old.md"
    old.write_text("# Old\n\nforgotten content\n", encoding="utf-8")
    ancient = time.time() - 200 * DAY
    os.utime(old, (ancient, ancient))
    (config.vault / "new.md").write_text("# New\n\nfresh content\n", encoding="utf-8")
    store = indexed()

    paths = {path for path, _ in stale_notes(store)}
    assert "old.md" in paths
    assert "new.md" not in paths


def test_pinned_notes_are_never_stale(config, indexed):
    import os

    target = config.vault / "old.md"
    target.write_text("# Old\n\ncontent\n", encoding="utf-8")
    ancient = time.time() - 200 * DAY
    os.utime(target, (ancient, ancient))
    store = indexed()
    store.set_pinned("old.md", True)
    store.commit()
    assert "old.md" not in {path for path, _ in stale_notes(store)}


# ------------------------------------------------------------- promotion --


def test_recurring_journal_topics_become_promotion_candidates(config, indexed):
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        journal = config.vault / "journal" / f"{day}.md"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(
            f"---\ntitle: Journal {day}\ntags: [auth]\n---\n\n# Journal {day}\n\n## 10:00\n\n決定事項。\n",
            encoding="utf-8",
        )
    store = indexed()
    tags = {c.tag: c.entries for c in promotion_candidates(store)}
    assert tags.get("auth") == 3


def test_topic_that_already_has_a_note_is_not_promoted_again(config, indexed):
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        journal = config.vault / "journal" / f"{day}.md"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(f"---\ntags: [auth]\n---\n\n# J {day}\n\nbody\n", encoding="utf-8")
    (config.vault / "auth.md").write_text("---\ntitle: auth\n---\n\n# auth\n\n決定の集約。\n", encoding="utf-8")
    store = indexed()
    assert "auth" not in {c.tag for c in promotion_candidates(store)}


def test_one_off_topic_is_not_promoted(config, indexed):
    journal = config.vault / "journal" / "2026-08-01.md"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("---\ntags: [oneoff]\n---\n\n# J\n\nbody\n", encoding="utf-8")
    store = indexed()
    assert "oneoff" not in {c.tag for c in promotion_candidates(store)}


# ------------------------------------------------------------- end to end --


def test_learn_writes_a_journal_entry_that_is_searchable(config):
    server = MemoryServer(config)
    _call(server, "memory_learn", {"content": "SQS を採用。運用コストが理由。", "tags": ["queue"]})
    text, error = _call(server, "memory_search", {"query": "SQS"})
    assert not error and "SQS" in text


def test_repeated_learning_appends_to_one_daily_file(config):
    server = MemoryServer(config)
    _call(server, "memory_learn", {"content": "最初の学び。", "tags": ["a"]})
    _call(server, "memory_learn", {"content": "二番目の学び。", "tags": ["b"]})

    journals = list((config.vault / "journal").glob("*.md"))
    assert len(journals) == 1
    body = journals[0].read_text(encoding="utf-8")
    assert "最初の学び" in body and "二番目の学び" in body
    # Tags from every entry accumulate on the day's note.
    assert "a" in body and "b" in body


def test_learn_requires_content(config):
    server = MemoryServer(config)
    text, _ = _call(server, "memory_learn", {"content": "   "})
    assert "required" in text


def test_review_reports_gaps_and_next_actions(config):
    server = MemoryServer(config)
    _call(server, "memory_learn", {"content": "認証は JWT。", "tags": ["auth"]})
    for _ in range(3):
        _call(server, "memory_search", {"query": "監視のアラート設定"})

    report = json.loads(_call(server, "memory_review")[0])
    assert any(g["query"] == "監視のアラート設定" for g in report["knowledge_gaps"])
    assert report["next_actions"] and "Nothing needs attention" not in report["next_actions"][0]


def test_review_is_quiet_when_nothing_is_wrong(config):
    server = MemoryServer(config)
    report = json.loads(_call(server, "memory_review")[0])
    assert report["next_actions"] == ["Nothing needs attention."]


def test_pinned_write_survives_as_pinned(config):
    server = MemoryServer(config)
    _call(server, "memory_write", {"path": "adr/core.md", "content": "重要な決定。", "pin": True})
    assert server.store.usage_map()["adr/core.md"][2] == 1


def test_growth_is_shared_between_agents(config):
    """What one agent learns, the other agent's ranking benefits from."""
    from bindery.config import Config

    claude_side = MemoryServer(config)
    _call(claude_side, "memory_learn", {"content": "デプロイは同一成果物を昇格させる。", "tags": ["deploy"]})
    for _ in range(4):
        _call(claude_side, "memory_search", {"query": "デプロイ"})

    codex_side = MemoryServer(
        Config.resolve(vault=config.vault, state_dir=config.state_dir, semantic=False)
    )
    hot = hot_paths(codex_side.store)
    assert hot and hot[0][1] >= 4


def test_superset_note_is_flagged_as_redundant(config, indexed):
    """The common real case: someone re-recorded a decision and added a line.

    Jaccard rates this well below a useful threshold; containment does not.
    """
    base = "デプロイは同一成果物を環境間で昇格させる。ビルドは一度だけ行う。"
    (config.vault / "a.md").write_text(f"# A\n\n{base}\n", encoding="utf-8")
    (config.vault / "b.md").write_text(f"# B\n\n{base} なお例外は無い。\n", encoding="utf-8")
    store = indexed()

    pairs = find_duplicates(store)
    assert {tuple(sorted((p.left, p.right))) for p in pairs} == {("a.md", "b.md")}


def test_short_passages_are_not_compared(config, indexed):
    """A one-liner shared by two notes is not evidence of redundancy."""
    (config.vault / "a.md").write_text("# A\n\nOK\n", encoding="utf-8")
    (config.vault / "b.md").write_text("# B\n\nOK\n", encoding="utf-8")
    store = indexed()
    assert find_duplicates(store) == []
