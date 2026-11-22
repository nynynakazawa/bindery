import type { BinderyApi, BinderySnapshot } from "./types";

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
    notes: 612,
    passages: 4_318,
    embedded: 4_302,
    semantic: "fastembed",
    state_dir: "/Users/you/.bindery",
    error: null,
  },
  clients: {
    clients: [
      { id: "claude", label: "Claude Code", detected: true, registered: true, config_path: "~/.claude.json", vault: "/Users/you/Obsidian/notes" },
      { id: "codex", label: "Codex", detected: true, registered: true, config_path: "~/.codex/config.toml", vault: "/Users/you/Obsidian/notes" },
      { id: "antigravity", label: "Antigravity CLI", detected: true, registered: false, config_path: "~/.gemini/config/mcp_config.json", vault: null },
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
    held_secrets: 0,
    scheduler: "installed",
    interval_seconds: 300,
    peers: ["macmini-9b1e07"],
    error: null,
  },
  refreshed_at: new Date().toISOString(),
};

const done = { ok: true, message: "preview" };

export function installDevPreview(): void {
  if (window.bindery) return;
  const kind = new URLSearchParams(location.search).get("window") === "settings" ? "settings" : "popover";
  const api: BinderyApi = {
    getWindowKind: () => kind,
    getSnapshot: async () => snapshot,
    syncNow: async () => ({ ...snapshot, refreshed_at: new Date().toISOString() }),
    openVault: async () => done,
    openLog: async () => done,
    showSettings: async () => undefined,
    checkUv: async () => ({ available: true, path: "/opt/homebrew/bin/uv", version: "uv 0.9", error: null }),
    installWheel: async () => done,
    selectVault: async () => snapshot.status.vault,
    installClients: async () => done,
    writeGlobalPrompt: async () => done,
    initSync: async () => done,
    pairSync: async () => done,
    installScheduler: async () => done,
  };
  window.bindery = api;
}
