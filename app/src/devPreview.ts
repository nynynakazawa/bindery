import type { BinderyApi, BinderySnapshot, NoteSummary } from "./types";

/**
 * Sample data for looking at the UI in a plain browser (`npm run dev`).
 * Only installed when there is no preload bridge, which is never the case
 * inside Electron, and the import is dropped from production builds.
 */
const snapshot: BinderySnapshot = {
  status: {
    version: "0.1.0",
    vault: "/Users/you/Obsidian/notes",
    vault_exists: true,
    indexed: true,
    notes: 8,
    passages: 87,
    embedded: 84,
    semantic: "fastembed",
    state_dir: "/Users/you/.bindery",
    error: null,
  },
  clients: {
    clients: [
      { id: "claude", label: "Claude Code", detected: true, registered: true, config_path: "~/.claude.json", vault: "/Users/you/Obsidian/notes" },
      { id: "codex", label: "Codex", detected: true, registered: true, config_path: "~/.codex/config.toml", vault: "/Users/you/Obsidian/notes" },
      { id: "antigravity", label: "Antigravity", detected: true, registered: true, config_path: "~/.gemini/config/mcp_config.json", vault: "/Users/you/Obsidian/notes" },
    ],
    connections: [
      { client: "claude", pid: 4123, started_at: new Date(Date.now() - 42 * 60_000).toISOString(), vault: "/Users/you/Obsidian/notes" },
      { client: "codex", pid: 5170, started_at: new Date(Date.now() - 6 * 60_000).toISOString(), vault: "/Users/you/Obsidian/notes" },
    ],
    error: null,
  },
  sync: {
    configured: true,
    remote: "macmini:bindery-sync/notes.git",
    machine: "macbook-3fa2c1",
    sync_id: "preview",
    last_success: new Date(Date.now() - 3 * 60_000).toISOString(),
    last_attempt: new Date(Date.now() - 3 * 60_000).toISOString(),
    git: "ok",
    index: "ok",
    last_error: null,
    blocked_reason: null,
    conflicts: 1,
    conflict_paths: ["notes/設計のメモ.conflict-macmini.md"],
    held_paths: [],
    secret_paths: ["arch/auth.md"],
    secret_count: 1,
    key_store: "keychain",
    held_secrets: 0,
    scheduler: "installed",
    interval_seconds: 300,
    peers: ["macmini-9b1e07"],
    error: null,
  },
  syncing: false,
  refreshed_at: new Date().toISOString(),
};

const examples = [
  ["arch/auth.md", "認証フローの設計判断", "Web App", ["auth", "API"], 42, 98],
  ["ops/checklist.md", "運用チェックリスト", "Bindery", ["運用", "Vault"], 31, 123],
  ["ui/theme.md", "MUI テーマの注意点", "Web App", ["UI", "MUI", "macOS"], 27, 62],
  ["ops/sync.md", "Vault 同期の設計", "Bindery", ["同期", "Vault"], 21, 56],
  ["notes/sqlite.md", "SQLite の索引更新", "Bindery", ["SQLite", "索引"], 18, 79],
  ["notes/cli.md", "CLI 登録の手順", "Bindery", ["CLI", "macOS"], 14, 32],
  ["arch/navigation.md", "ウィンドウ遷移", "Web App", ["Electron", "UI"], 9, 20],
  ["notes/logs.md", "同期ログの読み方", "Bindery", ["運用", "同期"], 4, 15],
] as const;
const notes: NoteSummary[] = examples.map(([path, title, project, tags, uses, impressions], index) => ({
  path, title, project, tags: [...tags], tier: index < 2 ? "core" : "reference", mtime: Date.now() - index * 35_000_000,
  chunks: 13 - index, uses, impressions, last_used: null,
}));
const entries = [
  ["2026-11-22", "SQLite WAL ロック対処", ["SQLite", "索引"]],
  ["2026-11-22", "CSP と外部画像の表示制御", ["Electron", "CSP"]],
  ["2026-11-21", "Electron 単一起動検証", ["Electron", "macOS"]],
  ["2026-11-20", "同期時の衝突コピー", ["同期", "Vault"]],
  ["2026-11-20", "初期設定ステップの整理", ["CLI"]],
  ["2026-11-19", "索引の件数を確認", ["SQLite"]],
].map(([date, heading, tags]) => ({ path: `journal/${date}.md`, date: String(date), heading: String(heading), tags: tags as string[] }));

const done = { ok: true, message: "preview" };

export function installDevPreview(): void {
  if (window.bindery) return;
  let loginItem = false;
  const api: BinderyApi = {
    getSnapshot: async () => snapshot,
    syncNow: async () => {
      await new Promise((resolve) => setTimeout(resolve, 600));
      const now = new Date().toISOString();
      snapshot.sync = { ...snapshot.sync, last_success: now, last_attempt: now };
      return { ...snapshot, refreshed_at: now };
    },
    openVault: async () => done,
    openLog: async () => done,
    checkUv: async () => ({ available: true, path: "/opt/homebrew/bin/uv", version: "uv 0.9", error: null }),
    installWheel: async () => done,
    selectVault: async () => snapshot.status.vault,
    installClients: async () => done,
    writeGlobalPrompt: async () => done,
    initSync: async () => done,
    pairSync: async () => done,
    installScheduler: async () => done,
    search: async (query) => ({
      results: notes.flatMap((note, index) => {
        const haystack = `${note.path} ${note.title} ${note.project} ${note.tags.join(" ")}`.toLowerCase();
        return haystack.includes(query.toLowerCase()) ? [{
          path: note.path, heading: "判断の理由", snippet: `${note.title}。${note.tags.join("、")}に関する設計判断と検証手順を記録しています。`,
          match: ["keyword", "semantic", "both"][index % 3], score: 1 - index / 20,
        }] : [];
      }), error: null,
    }),
    listNotes: async () => ({
      notes, totals: { notes: notes.length, chunks: 87, vectors: 84, links: 19, projects: { Bindery: 5, "Web App": 3 } }, error: null,
    }),
    readNote: async (path) => {
      if (!/\.md$/iu.test(path)) {
        return { error: "Expected a Markdown note", canReveal: snapshot.sync.held_paths?.includes(path) === true ||
          snapshot.sync.conflict_paths?.includes(path) === true };
      }
      const note = notes.find((note) => note.path === path);
      const entry = entries.find((entry) => entry.path === path);
      if (!note && !entry && !snapshot.sync.conflict_paths?.includes(path)) return { error: "File not found" };
      const title = note?.title ?? entry?.heading ?? "設計のメモ";
      return {
        path, title, mtime: note?.mtime ?? Date.now(), error: null,
        frontmatter: { project: note?.project ?? "Bindery", tier: note?.tier ?? "journal", tags: note?.tags ?? entry?.tags ?? [] },
        body: entry && !note ? `# 作業記録

${entries.filter((entry) => entry.path === path).map((entry) => `## ${entry.heading}

設計判断を記録しました。[[arch/auth|認証フローの設計判断]]も参照できます。`).join("\n\n")}` :
          `# ${title}

索引と同期の変更を確認するためのノートです。[[ops/checklist|運用手順]]と[同期の状態](../ops/sync.md#判断の理由)を参照してください。

## 判断の理由

既定の Vault に接続し、同期後に索引を確認します。必要に応じて [Electron のドキュメント](https://www.electronjs.org/docs/latest/)を開けます。

- [x] Vault を確認
- [ ] 同期結果を確認

| 対象 | 状態 |
| --- | --- |
| Vault | 接続済み |
| 索引 | 更新済み |

\`bindery status --json\` で現在の状態を確認できます。

\`\`\`sh
bindery status --json
\`\`\`

> 接続中のプロセス数は「接続」画面で確認します。

<div>生 HTML の見本</div>

![同期概要](https://example.com/diagram.png)
`,
      };
    },
    recent: async () => ({ entries, error: null }),
    ranking: async () => ({
      most_used: [...notes].sort((a, b) => b.uses - a.uses),
      most_shown: [...notes].sort((a, b) => b.impressions - a.impressions),
      top_queries: [{ text: "Vault", count: 18, avg_hits: 4.2 }, { text: "同期", count: 14, avg_hits: 3.7 }, { text: "SQLite", count: 9, avg_hits: 6.1 }],
      error: null,
    }),
    openInObsidian: async () => undefined,
    revealInFinder: async () => undefined,
    openExternal: async () => undefined,
    getLoginItem: async () => loginItem,
    setLoginItem: async (enabled) => { loginItem = enabled; return loginItem; },
  };
  window.bindery = api;
}
