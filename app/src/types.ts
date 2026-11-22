export type ClientId = "claude" | "codex" | "antigravity" | string;

export interface ClientInfo {
  id: ClientId;
  label: string;
  detected: boolean;
  registered: boolean;
  config_path: string;
  vault: string | null;
}

export interface ConnectionInfo {
  client: "claude" | "codex" | "antigravity" | "unknown";
  pid: number;
  started_at: string | null;
  vault: string | null;
}

export interface StatusInfo {
  version: string;
  vault: string;
  vault_exists: boolean;
  indexed: boolean;
  notes: number | null;
  passages: number | null;
  embedded: number | null;
  semantic: string;
  state_dir: string;
  error: string | null;
}

export interface ClientsInfo {
  clients: ClientInfo[];
  connections: ConnectionInfo[];
  error: string | null;
}

export type SyncGitState = "ok" | "error" | "blocked" | "never";
export type SyncIndexState = "ok" | "error" | "skipped" | "never";
export type SchedulerState = "installed" | "absent";

export interface SyncInfo {
  configured: boolean;
  remote: string | null;
  machine: string | null;
  sync_id: string | null;
  last_success: string | null;
  last_attempt: string | null;
  git: SyncGitState;
  index: SyncIndexState;
  last_error: string | null;
  blocked_reason: string | null;
  conflicts: number;
  held_secrets: number;
  scheduler: SchedulerState;
  interval_seconds: number | null;
  peers: string[];
  error: string | null;
}

export interface BinderySnapshot {
  status: StatusInfo;
  clients: ClientsInfo;
  sync: SyncInfo;
  refreshed_at: string;
}

export interface CommandResult {
  ok: boolean;
  message: string;
  output?: string;
}

export interface UvInfo {
  available: boolean;
  path: string | null;
  version: string | null;
  error: string | null;
}

export interface BinderyApi {
  getWindowKind(): "popover" | "settings";
  getSnapshot(): Promise<BinderySnapshot>;
  syncNow(): Promise<BinderySnapshot>;
  openVault(): Promise<CommandResult>;
  openLog(): Promise<CommandResult>;
  showSettings(): Promise<void>;
  checkUv(): Promise<UvInfo>;
  installWheel(): Promise<CommandResult>;
  selectVault(): Promise<string | null>;
  installClients(): Promise<CommandResult>;
  writeGlobalPrompt(): Promise<CommandResult>;
  initSync(remote: string): Promise<CommandResult>;
  pairSync(remote: string): Promise<CommandResult>;
  installScheduler(): Promise<CommandResult>;
}

declare global {
  interface Window {
    bindery: BinderyApi;
  }
}

