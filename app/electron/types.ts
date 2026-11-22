export type KnownClient = "claude" | "codex" | "antigravity";

export interface ClientInfo {
  id: string;
  label: string;
  detected: boolean;
  registered: boolean;
  config_path: string;
  vault: string | null;
}

export interface ConnectionInfo {
  client: KnownClient | "unknown";
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
  key_store?: "keychain" | "file" | null;
  last_success: string | null;
  last_attempt: string | null;
  git: SyncGitState;
  index: SyncIndexState;
  last_error: string | null;
  blocked_reason: string | null;
  conflicts: number;
  held_secrets: number;
  held_paths?: string[];
  secret_count?: number;
  secret_paths?: string[];
  conflict_paths?: string[];
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
  syncing: boolean;
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

export interface SearchEntry {
  path: string;
  heading: string | null;
  snippet: string;
  match: string;
  score: number;
}

export interface NoteSummary {
  path: string;
  title: string;
  project: string | null;
  tags: string[];
  tier: string | null;
  mtime: number | string | null;
  chunks: number;
  uses: number;
  impressions: number;
  last_used: number | string | null;
}

export interface NotesData {
  notes: NoteSummary[];
  totals: { notes: number; chunks: number; vectors: number; links: number; projects: Record<string, number> };
}

export interface RecentEntry {
  path: string;
  heading: string;
  date: string;
  tags: string[];
}

export interface RankingData {
  most_used: Array<{ path: string; title: string; uses: number }>;
  most_shown: Array<{ path: string; title: string; impressions: number }>;
  top_queries: Array<{ text: string; count: number; avg_hits: number }>;
}

export interface NoteDocument {
  path: string;
  title: string;
  frontmatter: Record<string, unknown>;
  body: string;
  mtime: number | string | null;
}

export type CliResponse<T> = (T & { error: null }) | { error: string };
export type NoteResponse = CliResponse<NoteDocument> & { canReveal?: boolean };

export interface BinderyApi {
  getSnapshot(): Promise<BinderySnapshot>;
  syncNow(): Promise<BinderySnapshot>;
  openVault(): Promise<CommandResult>;
  openLog(): Promise<CommandResult>;
  checkUv(): Promise<UvInfo>;
  installWheel(): Promise<CommandResult>;
  selectVault(): Promise<string | null>;
  installClients(): Promise<CommandResult>;
  writeGlobalPrompt(): Promise<CommandResult>;
  initSync(remote: string): Promise<CommandResult>;
  pairSync(remote: string): Promise<CommandResult>;
  installScheduler(): Promise<CommandResult>;
  search(query: string): Promise<CliResponse<{ results: SearchEntry[] }>>;
  listNotes(): Promise<CliResponse<NotesData>>;
  readNote(relPath: string): Promise<NoteResponse>;
  recent(): Promise<CliResponse<{ entries: RecentEntry[] }>>;
  ranking(): Promise<CliResponse<RankingData>>;
  openInObsidian(relPath: string): Promise<void>;
  revealInFinder(relPath: string): Promise<void>;
  openExternal(url: string): Promise<void>;
  setLoginItem(enabled: boolean): Promise<boolean>;
  getLoginItem(): Promise<boolean>;
}

declare global {
  interface Window {
    bindery: BinderyApi;
  }
}

