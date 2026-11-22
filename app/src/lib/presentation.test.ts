import { describe, expect, it } from "vitest";
import type { ClientsInfo, SyncInfo } from "../types";
import { formatClientState, formatConnectedCliNames, formatSyncState } from "./presentation";

const clients: ClientsInfo = {
  clients: [
    { id: "claude", label: "Claude Code", detected: true, registered: true, config_path: "", vault: null },
    { id: "codex", label: "Codex", detected: true, registered: false, config_path: "", vault: null },
  ],
  connections: [
    { client: "codex", pid: 10, started_at: null, vault: null },
    { client: "codex", pid: 11, started_at: null, vault: null },
    { client: "claude", pid: 12, started_at: null, vault: null },
  ],
  error: null,
};

const sync = (overrides: Partial<SyncInfo> = {}): SyncInfo => ({
  configured: true,
  remote: null,
  machine: null,
  sync_id: null,
  last_success: null,
  last_attempt: null,
  git: "never",
  index: "never",
  last_error: null,
  blocked_reason: null,
  conflicts: 0,
  held_secrets: 0,
  scheduler: "absent",
  interval_seconds: null,
  peers: [],
  error: null,
  ...overrides,
});

describe("renderer presentation helpers", () => {
  it("deduplicates and orders connected CLI names by observed connection order", () => {
    expect(formatConnectedCliNames(clients)).toEqual(["Codex", "Claude Code"]);
  });

  it("maps blocked and error states before healthy and never states", () => {
    expect(formatSyncState(sync())).toBe("never");
    expect(formatSyncState(sync({ git: "ok" }))).toBe("ok");
    expect(formatSyncState(sync({ git: "error" }))).toBe("error");
    expect(formatSyncState(sync({ git: "ok", blocked_reason: "tampered" }))).toBe("blocked");
  });

  it("prioritizes connection, registration, detection, then missing", () => {
    expect(formatClientState(clients.clients[0], clients.connections, "claude")).toBe("connected");
    expect(formatClientState(clients.clients[0], [], "claude")).toBe("registered");
    expect(formatClientState(clients.clients[1], [], "codex")).toBe("detected");
    expect(formatClientState({ detected: false, registered: false }, [], "antigravity")).toBe("missing");
  });
});
