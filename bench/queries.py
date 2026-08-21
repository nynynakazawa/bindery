"""What the benchmark asks, and what counts as right.

Each case names the note that should come back. The categories are the failure
modes this project has actually had, so a regression in any of them shows up
as a number rather than as a complaint months later.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Case:
    category: str
    query: str
    #: Paths that answer the question. Any of them counts as a hit.
    expect: list[str]
    #: The project the agent is working in when asking.
    project: str = "alpha"
    #: Paths that must NOT appear. Used for leakage, not for ranking.
    forbid: list[str] = field(default_factory=list)


CASES: list[Case] = [
    # ---- exact identifiers: what lexical search is for --------------------
    Case("exact-identifier", "BINDERY_MAX_TOKENS", ["alpha/config-reference.md"]),
    Case("exact-identifier", "API_RATE_LIMIT_PER_MINUTE", ["alpha/rate-limit.md"]),
    Case(
        "exact-error",
        "TypeError: encode() got an unexpected keyword argument",
        ["journal/alpha/2026-07-03.md"],
    ),
    Case("exact-error", "database is locked", ["journal/alpha/2026-06-11.md"]),

    # ---- paraphrase: what semantic search is for --------------------------
    Case("paraphrase-ja", "複垢防止ってどうやってた？", ["alpha/auth.md"]),
    Case("paraphrase-ja", "リリースのとき成果物はどう扱う方針だった？", ["alpha/deploy.md", "alpha/deploy-notes.md"]),
    Case("paraphrase-en", "how do we identify students uniquely", ["alpha/auth.md"]),

    # ---- a query one word longer than the note ---------------------------
    Case("longer-query-ja", "大学メールを使う理由は何だった", ["alpha/auth.md"]),
    Case("longer-query-ja", "WAL が有効にできなかった原因", ["journal/alpha/2026-06-11.md"]),

    # ---- short CJK, which the trigram index cannot match alone ------------
    Case("short-cjk", "認証", ["alpha/auth.md"]),
    Case("short-cjk", "課金", []),   # nothing to find; must not invent one

    # ---- cross-language: asked in one, written in the other --------------
    Case("cross-language", "editor tooling install cache", ["tooling.md"]),
    Case("cross-language", "リフレッシュ間隔", ["beta/auth.md", "journal/episodes/beta/2026-07-22-codex-ef56ab78.md"], project="beta"),

    # ---- failures, which nothing in the repository records ---------------
    Case("past-failure", "マイグレーションが止まる問題", ["journal/episodes/alpha/2026-07-20-claude-ab12cd34.md"]),

    # ---- current vs superseded decision ----------------------------------
    Case(
        "current-decision", "いまのDBは何を使っている",
        ["alpha/database.md"],
    ),

    # ---- project isolation, the loudest failure mode ----------------------
    Case(
        "project-isolation", "認証はどうする方針だった",
        ["alpha/auth.md"], project="alpha", forbid=["beta/auth.md"],
    ),
    Case(
        "project-isolation", "authentication approach",
        ["beta/auth.md"], project="beta", forbid=["alpha/auth.md"],
    ),

    # ---- knowledge that belongs to no project ----------------------------
    Case("global-fact", "ログに出してはいけないもの", ["conventions.md"]),
    Case("global-fact", "ログに出してはいけないもの", ["conventions.md"], project="beta"),
]
