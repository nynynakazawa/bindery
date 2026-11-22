import type { ClientsInfo, ConnectionInfo, SyncInfo } from "../types";

const defaultLabels: Record<string, string> = {
  claude: "Claude Code",
  codex: "Codex",
  antigravity: "Antigravity",
};

export function formatConnectedCliNames(clients: ClientsInfo): string[] {
  const labels = new Map(clients.clients.map((client) => [client.id, client.label]));
  const names = clients.connections.map(
    (connection) => labels.get(connection.client) ?? defaultLabels[connection.client] ?? connection.client,
  );
  return [...new Set(names)];
}

export function formatSyncState(sync: SyncInfo): "ok" | "error" | "blocked" | "never" {
  if (sync.blocked_reason || sync.git === "blocked") return "blocked";
  if (sync.git === "error" || sync.index === "error" || sync.error || sync.last_error) return "error";
  if (sync.git === "ok") return "ok";
  return "never";
}

export function formatClientState(
  client: { detected: boolean; registered: boolean },
  connections: ConnectionInfo[],
  clientId: string,
): "connected" | "registered" | "detected" | "missing" {
  if (connections.some((connection) => connection.client === clientId)) return "connected";
  if (client.registered) return "registered";
  if (client.detected) return "detected";
  return "missing";
}
