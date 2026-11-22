import { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, shell, Tray } from "electron";
import { appendFile, mkdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import {
  binderyCandidates,
  executableExists,
  findExecutable,
  findWheel,
  readSavedBinderyPath,
  runAbsoluteCommand,
  runJsonCommand,
  saveBinderyPath,
  uvCandidates,
} from "./commands";
import { IPC_CHANNELS, assertAbsolutePath, configureRendererTrust, parseExternalUrl, validateIpcRequest } from "./security";
import type { IpcChannel } from "./channels";
import { resolveVaultFile } from "./vaultPath";
import type {
  BinderySnapshot, ClientsInfo, CliResponse, CommandResult, NoteDocument, NoteResponse,
  NotesData, RankingData, RecentEntry, SearchEntry, StatusInfo, SyncInfo,
} from "./types";

const emptyStatus = (error: string): StatusInfo => ({ version: "unknown", vault: "", vault_exists: false, indexed: false, notes: null, passages: null, embedded: null, semantic: "unknown", state_dir: "", error });
const emptyClients = (error: string): ClientsInfo => ({ clients: [], connections: [], error });
const emptySync = (error: string): SyncInfo => ({ configured: false, remote: null, machine: null, sync_id: null, last_success: null, last_attempt: null, git: "never", index: "never", last_error: null, blocked_reason: null, conflicts: 0, held_secrets: 0, scheduler: "absent", interval_seconds: null, peers: [], error });

let tray: Tray | null = null;
let dashboard: BrowserWindow | null = null;
let binderyPath: string | null = null;
let selectedVault: string | null = null;
let lastSnapshot: BinderySnapshot | null = null;
let pendingBinderyPath: Promise<string | null> | null = null;
let pendingSync: Promise<BinderySnapshot> | null = null;
let failedSync: { attempted: string | null; succeeded: string | null; syncId: string | null } | null = null;

function userDataPath(name: string): string {
  return path.join(app.getPath("userData"), name);
}

function assetPath(name: string): string {
  return path.join(app.getAppPath(), "build-assets", name);
}

function safeNativeImage(name: string): Electron.NativeImage {
  try {
    const image = nativeImage.createFromPath(assetPath(name));
    image.setTemplateImage(true);
    return image;
  } catch {
    return nativeImage.createEmpty();
  }
}

async function appendSafeLog(message: string): Promise<void> {
  try {
    const logPath = path.join(app.getPath("logs"), "bindery-app.log");
    await mkdir(path.dirname(logPath), { recursive: true });
    await appendFile(logPath, `${new Date().toISOString()} ${message.replace(/[\r\n]/gu, " ").slice(0, 500)}\n`, { mode: 0o600 });
  } catch {
    // Logging must never make an IPC operation fail.
  }
}

async function ensureBinderyPath(): Promise<string | null> {
  if (binderyPath && await executableExists(binderyPath)) return binderyPath;
  pendingBinderyPath ??= (async () => {
    const saved = await readSavedBinderyPath(userDataPath("bindery-path.json"));
    binderyPath = saved ?? await findExecutable(binderyCandidates());
    if (binderyPath && !saved) {
      await saveBinderyPath(userDataPath("bindery-path.json"), binderyPath).catch(() => {
        void appendSafeLog("Could not save Bindery executable path");
      });
    }
    return binderyPath;
  })().finally(() => { pendingBinderyPath = null; });
  return pendingBinderyPath;
}

async function runBindery(args: string[]): Promise<{ output: Awaited<ReturnType<typeof runAbsoluteCommand>>; executable: string | null }> {
  const executable = await ensureBinderyPath();
  if (!executable) {
    return { executable: null, output: { stdout: "", stderr: "Bindery executable not found", code: 127 } };
  }
  const output = await runAbsoluteCommand(executable, args);
  void appendSafeLog(`bindery ${args[0] ?? "operation"} exited=${output.code}`);
  return { executable, output };
}

async function binderyJson<T>(args: string[], fallback: T, options?: { maxBuffer?: number; timeout?: number }): Promise<T> {
  const executable = await ensureBinderyPath();
  if (!executable) return fallback;
  const result = await runJsonCommand<T>(executable, args, options);
  if (result.value && typeof result.value === "object") return result.value;
  return fallback;
}

async function getStatus(): Promise<StatusInfo> {
  const fallback = emptyStatus("Bindery status unavailable");
  return binderyJson<StatusInfo>(["status", "--json"], fallback);
}

async function getClients(): Promise<ClientsInfo> {
  return binderyJson<ClientsInfo>(["clients", "--json"], emptyClients("Bindery clients unavailable"));
}

async function getSync(fallback = emptySync("Bindery sync status unavailable")): Promise<SyncInfo> {
  return binderyJson<SyncInfo>(["sync", "status", "--json"], fallback);
}

async function getSnapshot(syncResult?: SyncInfo): Promise<BinderySnapshot> {
  const [status, clients, sync] = await Promise.all([getStatus(), getClients(), syncResult ?? getSync()]);
  if (failedSync && (sync.last_attempt !== failedSync.attempted ||
      sync.last_success !== failedSync.succeeded || sync.sync_id !== failedSync.syncId)) failedSync = null;
  const snapshot = {
    status,
    clients,
    sync: failedSync ? { ...sync, error: "Sync could not be completed" } : sync,
    syncing: !syncResult && Boolean(pendingSync),
    refreshed_at: new Date().toISOString(),
  };
  lastSnapshot = snapshot;
  updateTray();
  return snapshot;
}

function syncNow(): Promise<BinderySnapshot> {
  if (pendingSync) return pendingSync;
  failedSync = null;
  pendingSync = (async () => {
    const result = await binderyJson<{ exit_code: number; status?: SyncInfo }>(["sync", "--json"], { exit_code: 1 }, { timeout: 120_000 });
    const sync = await getSync(result.status);
    if (!result.status || (result.exit_code !== 0 && !sync.error && !sync.last_error && !sync.blocked_reason)) {
      failedSync = { attempted: sync.last_attempt, succeeded: sync.last_success, syncId: sync.sync_id };
    }
    void appendSafeLog(`bindery sync exited=${result.exit_code}`);
    return getSnapshot(sync);
  })().finally(() => {
    pendingSync = null;
    updateTray();
  });
  updateTray();
  return pendingSync;
}

function updateTray(): void {
  if (!tray) return;
  const active = Boolean(pendingSync || lastSnapshot?.clients.connections.length);
  tray.setImage(safeNativeImage(active ? "trayActiveTemplate.png" : "trayTemplate.png"));
}

function createDashboard(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 880,
    minHeight: 560,
    title: "Bindery",
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 16, y: 16 },
    show: false,
    backgroundColor: "#000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.on("will-redirect", (event) => event.preventDefault());
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.on("closed", () => { dashboard = null; });
  const loaded = app.isPackaged
    ? window.loadFile(path.join(app.getAppPath(), "dist", "index.html"))
    : window.loadURL("http://localhost:5173");
  void loaded.catch(() => { void appendSafeLog("Dashboard renderer could not be loaded"); });
  return window;
}

function showDashboard(): void {
  if (!app.isReady()) return;
  dashboard ??= createDashboard();
  if (dashboard.isMinimized()) dashboard.restore();
  if (process.platform === "darwin") app.focus({ steal: true });
  dashboard.show();
  dashboard.focus();
}

const DASHBOARD_OUTPUT_LIMIT = 16 * 1024 * 1024;

function dashboardJson<T>(args: string[], error: string): Promise<CliResponse<T>> {
  return binderyJson<CliResponse<T>>(args, { error }, { maxBuffer: DASHBOARD_OUTPUT_LIMIT });
}

async function resolveNoteActionPath(relative: string, status?: StatusInfo): Promise<string> {
  // Note commands use the registered Vault, independently of a pending setup selection.
  // Another local process can replace a path component after validation and before the OS opens it.
  const { vault, vault_exists } = status ?? await getStatus();
  if (!vault || !vault_exists) throw new Error("No registered Vault");
  assertAbsolutePath(vault);
  return resolveVaultFile(vault, relative);
}

async function readNote(relative: string): Promise<NoteResponse> {
  const markdown = /\.md$/iu.test(relative);
  const note = markdown
    ? await dashboardJson<NoteDocument>(["read", "--json", "--", relative], "Could not read the note")
    : { error: "Expected a Markdown note" };
  if (note.error === null) return note;
  // The review list comes from a snapshot that already identifies its Vault.
  const status = markdown ? undefined : lastSnapshot?.status;
  const canReveal = (markdown || status !== undefined) &&
    await resolveNoteActionPath(relative, status).then(() => true, () => false);
  return { ...note, canReveal };
}

function getConfiguredVault(status: StatusInfo): string {
  const vault = selectedVault ?? status.vault;
  if (!vault) throw new Error("Select a Vault first");
  assertAbsolutePath(vault);
  return path.resolve(vault);
}

async function operationResult(operation: string, args: string[]): Promise<CommandResult> {
  const result = await runBindery(args);
  if (result.output.code === 0) return { ok: true, message: `${operation} completed`, output: result.output.stdout.slice(-1000) };
  return { ok: false, message: result.output.stderr.trim().slice(-500) || `${operation} failed`, output: result.output.stdout.slice(-1000) };
}

async function installClients(): Promise<CommandResult> {
  const vault = getConfiguredVault(await getStatus());
  for (const client of ["claude", "codex", "antigravity"]) {
    const result = await runBindery(["install", client, "--write", "--vault", vault]);
    if (result.output.code !== 0) return { ok: false, message: result.output.stderr.trim().slice(-500) || `install ${client} failed` };
  }
  return { ok: true, message: "clients installed" };
}

function setupWheelDirectory(): string {
  return app.isPackaged ? path.join(process.resourcesPath, "wheel") : path.resolve(__dirname, "..", "..", "dist-wheel");
}

function registerIpc(): void {
  const guarded = <Args extends [] | [string] | [boolean], T>(channel: IpcChannel, action: (...args: Args) => T): void => {
    ipcMain.handle(channel, (event, ...args: unknown[]) => {
      if (event.sender !== dashboard?.webContents || event.senderFrame?.parent !== null || args.length > 1) {
        throw new Error("Invalid IPC invocation");
      }
      validateIpcRequest(channel, args[0], event.senderFrame.url);
      return action(...args as Args);
    });
  };

  guarded(IPC_CHANNELS.getSnapshot, () => getSnapshot());
  guarded(IPC_CHANNELS.syncNow, syncNow);
  guarded(IPC_CHANNELS.search, (query: string) => query.trim()
    ? dashboardJson<{ results: SearchEntry[] }>(["search", "--json", "--", query], "Search unavailable")
    : { results: [], error: null });
  guarded(IPC_CHANNELS.listNotes, () => dashboardJson<NotesData>(["notes", "--json"], "Note index unavailable"));
  guarded(IPC_CHANNELS.readNote, readNote);
  guarded(IPC_CHANNELS.recent, () => dashboardJson<{ entries: RecentEntry[] }>(["recent", "--json", "--limit", "20"], "Recent records unavailable"));
  guarded(IPC_CHANNELS.ranking, () => dashboardJson<RankingData>(["ranking", "--json", "--limit", "10"], "Rankings unavailable"));
  guarded(IPC_CHANNELS.openInObsidian, async (relative: string) => {
    const target = await resolveNoteActionPath(relative);
    if (!/\.md$/iu.test(target)) throw new Error("Expected a Markdown note");
    await shell.openExternal(`obsidian://open?path=${encodeURIComponent(target)}`);
  });
  guarded(IPC_CHANNELS.revealInFinder, async (relative: string) => {
    shell.showItemInFolder(await resolveNoteActionPath(relative));
  });
  guarded(IPC_CHANNELS.openExternal, (url: string) => shell.openExternal(parseExternalUrl(url).href));
  guarded(IPC_CHANNELS.getLoginItem, () => app.getLoginItemSettings().openAtLogin);
  guarded(IPC_CHANNELS.setLoginItem, (enabled: boolean) => {
    app.setLoginItemSettings({ openAtLogin: enabled });
    const settings = app.getLoginItemSettings();
    if (settings.openAtLogin !== enabled || (enabled && settings.status === "requires-approval")) {
      throw new Error("Login item setting was not approved");
    }
    return settings.openAtLogin;
  });
  guarded(IPC_CHANNELS.openVault, async () => {
    const status = await getStatus();
    const target = status.vault;
    try {
      assertAbsolutePath(target);
      await stat(target);
      const message = await shell.openPath(target);
      return { ok: !message, message };
    } catch (error) {
      return { ok: false, message: error instanceof Error ? error.message : "Vault could not be opened" };
    }
  });
  guarded(IPC_CHANNELS.openLog, async () => {
    const target = path.join(app.getPath("logs"), "bindery-app.log");
    try {
      await mkdir(path.dirname(target), { recursive: true });
      await appendFile(target, "", { mode: 0o600 });
      const message = await shell.openPath(target);
      return { ok: !message, message };
    } catch (error) {
      return { ok: false, message: error instanceof Error ? error.message : "Log could not be opened" };
    }
  });
  guarded(IPC_CHANNELS.checkUv, async () => {
    const uvPath = await findExecutable(uvCandidates());
    if (!uvPath) return { available: false, path: null, version: null, error: "uv was not found in the supported locations" };
    const result = await runAbsoluteCommand(uvPath, ["--version"]);
    return { available: result.code === 0, path: uvPath, version: result.stdout.trim() || null, error: result.code === 0 ? null : result.stderr.trim() || "uv check failed" };
  });
  guarded(IPC_CHANNELS.installWheel, async () => {
    const uvPath = await findExecutable(uvCandidates());
    const wheel = await findWheel(setupWheelDirectory());
    if (!uvPath) return { ok: false, message: "uv was not found" };
    if (!wheel) return { ok: false, message: "Bundled Bindery wheel was not found" };
    const result = await runAbsoluteCommand(uvPath, ["tool", "install", wheel]);
    if (result.code !== 0) return { ok: false, message: result.stderr.trim().slice(-500) || "wheel installation failed" };
    binderyPath = await findExecutable(binderyCandidates());
    if (binderyPath) await saveBinderyPath(userDataPath("bindery-path.json"), binderyPath);
    return binderyPath ? { ok: true, message: "Bindery wheel installed" } : { ok: false, message: "Bindery installed but executable was not found" };
  });
  guarded(IPC_CHANNELS.selectVault, async () => {
    const parent = dashboard;
    const result = parent
      ? await dialog.showOpenDialog(parent, { properties: ["openDirectory", "createDirectory"] })
      : await dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
    const chosen = result.canceled ? null : result.filePaths[0] ?? null;
    if (chosen) { assertAbsolutePath(chosen); selectedVault = path.resolve(chosen); }
    return chosen;
  });
  guarded(IPC_CHANNELS.installClients, () => installClients());
  guarded(IPC_CHANNELS.writeGlobalPrompt, () => operationResult("prompt", ["prompt", "--global", "--write"]));
  guarded(IPC_CHANNELS.initSync, async (remote: string) => {
    const vault = getConfiguredVault(await getStatus());
    return operationResult("sync init", ["sync", "init", "--remote", remote, "--vault", vault, "--write"]);
  });
  guarded(IPC_CHANNELS.pairSync, async (remote: string) => {
    const vault = getConfiguredVault(await getStatus());
    return operationResult("sync pair", ["sync", "pair", remote, "--vault", vault]);
  });
  guarded(IPC_CHANNELS.installScheduler, async () => {
    const vault = getConfiguredVault(await getStatus());
    return operationResult("sync install", ["sync", "install", "--vault", vault, "--write"]);
  });
}

async function createTray(): Promise<void> {
  const language = app.getLocale().toLowerCase().startsWith("en") ? "en" : "ja";
  const copy = JSON.parse(await readFile(path.join(app.getAppPath(), "src", "copy", `${language}.json`), "utf8")) as Record<string, string>;
  tray = new Tray(safeNativeImage("trayTemplate.png"));
  tray.setToolTip("Bindery");
  const menu = Menu.buildFromTemplate([
    { label: copy["actions.openBindery"], click: showDashboard },
    { label: copy["actions.syncNow"], click: () => { void syncNow().catch(() => { void appendSafeLog("Tray sync failed"); }); } },
    { type: "separator" },
    { label: copy["actions.quit"], role: "quit" },
  ]);
  tray.on("click", showDashboard);
  tray.on("right-click", () => tray?.popUpContextMenu(menu));
  updateTray();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showDashboard);
  app.on("activate", showDashboard);
  app.on("window-all-closed", () => {
    // The tray remains available after the dashboard is closed.
  });
  app.on("will-quit", () => { tray?.destroy(); tray = null; });

  void app.whenReady().then(() => {
    configureRendererTrust({ packaged: app.isPackaged, indexPath: path.join(app.getAppPath(), "dist", "index.html") });
    registerIpc();
    if (process.platform === "darwin") {
      Menu.setApplicationMenu(Menu.buildFromTemplate([
        { role: "appMenu" }, { role: "fileMenu" }, { role: "editMenu" }, { role: "windowMenu" },
      ]));
    }
    showDashboard();
    void createTray().catch(() => { void appendSafeLog("Tray could not be created"); });
  });
}
