"""A benchmark corpus shaped like a coding agent's memory, not a QA dataset.

The public long-context benchmarks measure recall over conversational history.
That is not what this retrieves. What this retrieves is a small, messy,
bilingual pile of decisions, constraints, dead ends, and superseded choices,
queried in whichever language the developer was thinking in - and the failure
modes that matter are specific to that: an exact identifier not matching, a
Japanese question one word longer than the note, a decision from the wrong
project answering with total confidence.

So the corpus is written to contain those traps deliberately. Every note here
exists because some query in `queries.py` should - or specifically should not
- return it.
"""

from __future__ import annotations

from pathlib import Path

#: (path, project, tier, title, body)
NOTES: list[tuple[str, str, str, str, str]] = [
    # ---- durable decisions, the thing a search should usually find --------
    (
        "alpha/auth.md", "alpha", "durable", "認証方式の決定",
        "学生の本人性確認には大学メールアドレスを一意識別子として使う。\n"
        "SMS認証は費用と離脱率の両面で不利と判断して却下した。\n"
        "同一メールでの再登録は複垢とみなして拒否する。",
    ),
    (
        "alpha/rate-limit.md", "alpha", "durable", "レート制限",
        "公開APIは1分あたり100リクエストに制限する。\n"
        "設定は環境変数 BINDERY_MAX_TOKENS とは無関係で、"
        "API_RATE_LIMIT_PER_MINUTE で指定する。",
    ),
    (
        "alpha/deploy.md", "alpha", "durable", "デプロイ方針",
        "同一のビルド成果物を環境間で昇格させる。ビルドは一度だけ行う。\n"
        "本番反映前にヘルスチェックを通し、失敗したらロールバックする。",
    ),
    (
        "beta/auth.md", "beta", "durable", "Authentication for beta",
        "beta uses Clerk for authentication. Sessions are refreshed every 30 days.\n"
        "This is a different product from alpha and shares no user table.",
    ),
    # ---- a decision that was later reversed -------------------------------
    (
        "alpha/database.md", "alpha", "durable", "データベース選定",
        "PostgreSQL から SQLite へ移行した。単一ノードで十分であり、"
        "運用コストが見合わないと判断したため。\n"
        "現在の正式な選択は SQLite である。",
    ),
    (
        "journal/alpha/2026-05-02.md", "alpha", "journal", "Journal 2026-05-02",
        "## 10:00\n\nDBは PostgreSQL を採用することにした。"
        "マネージドサービスがあり運用が楽なため。",
    ),
    # ---- a failure, which nothing in the repository records ---------------
    (
        "journal/alpha/2026-06-11.md", "alpha", "journal", "Journal 2026-06-11",
        "## 14:20\n\nWAL を有効にしようとして database is locked で落ちた。\n"
        "原因は PRAGMA busy_timeout を journal_mode より後に設定していたこと。\n"
        "busy_timeout を先に設定したら解決した。",
    ),
    # ---- cross-project knowledge, findable from anywhere ------------------
    (
        "conventions.md", "", "durable", "共通の規約",
        "秘密情報はログに出さない。エラーは握り潰さず、"
        "利用者向けの表示と運用者向けの診断を分ける。",
    ),
    (
        "tooling.md", "", "durable", "Editor and tooling",
        "uv tool install --force reuses a cached build. "
        "Use --reinstall when the version number has not changed.",
    ),
    # ---- an exact identifier and an exact error string --------------------
    (
        "alpha/config-reference.md", "alpha", "durable", "設定リファレンス",
        "BINDERY_MAX_TOKENS は検索1回あたりの応答トークン上限。既定は2000。\n"
        "BINDERY_CHUNK_TOKENS はチャンクの目標サイズ。",
    ),
    (
        "journal/alpha/2026-07-03.md", "alpha", "journal", "Journal 2026-07-03",
        "## 09:15\n\nTypeError: encode() got an unexpected keyword argument "
        "'normalize_embeddings' が出た。fastembed と sentence-transformers で"
        "APIが違うのが原因。バックエンドごとに分岐させた。",
    ),
    # ---- an episode: nobody wrote this down on purpose --------------------
    (
        "journal/episodes/alpha/2026-07-20-claude-ab12cd34.md", "alpha", "episode",
        "本番のマイグレーションが途中で止まる",
        "**User:** 本番のマイグレーションが途中で止まる。原因を調べてほしい。\n\n"
        "調べます。\n\n```\n$ psql -c 'select * from pg_stat_activity'\n```\n\n"
        "ロック待ちが発生していました。長時間トランザクションが原因です。\n\n"
        "**User:** どう直した?\n\n"
        "マイグレーションをバッチに分割し、1バッチごとにコミットするようにしました。",
    ),
    (
        "journal/episodes/beta/2026-07-22-codex-ef56ab78.md", "beta", "episode",
        "Clerk のセッションが切れる",
        "**User:** Clerk のセッションがすぐ切れる。\n\n"
        "リフレッシュ間隔の設定が既定のままでした。30日に変更します。",
    ),
    # ---- near-duplicates, to catch a ranking that returns the same thing --
    (
        "alpha/deploy-notes.md", "alpha", "durable", "デプロイ補足",
        "同一のビルド成果物を環境間で昇格させる。ビルドは一度だけ行う。\n"
        "なお例外は無い。",
    ),
    # ---- filler, so that ranking has something to be wrong about ----------
    *[
        (
            f"alpha/misc-{i:02d}.md", "alpha", "durable", f"補足メモ {i}",
            f"これは {i} 番目の補足メモである。"
            "特定の質問に答えるためのものではなく、検索空間を埋めるために存在する。\n"
            "UI の余白、ログの書式、命名の細かい揺れなどについて書かれている。",
        )
        for i in range(30)
    ],
    *[
        (
            f"beta/misc-{i:02d}.md", "beta", "durable", f"Filler note {i}",
            f"This is filler note {i} for the beta project. "
            "It exists so that retrieval has a crowded space to be wrong in, "
            "and mentions deployment, configuration, and testing in passing.",
        )
        for i in range(30)
    ],
]


def build(vault: Path) -> int:
    """Write the corpus into an empty vault."""
    for rel, project, _tier, title, body in NOTES:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\ntitle: {title}\nproject: {project}\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )
    return len(NOTES)
