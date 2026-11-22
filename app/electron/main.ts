import { app, BrowserWindow, dialog, ipcMain, nativeImage, shell, Tray } from "electron";
import { appendFile, mkdir, readFile, stat } from "node:fs/promises";
import { constants } from "node:fs";
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
import { IPC_CHANNELS, assertAbsolutePath, configureRendererTrust, validateIpcRequest } from "./security";

interface StatusInfo {
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

interface ClientsInfo {
  clients: Array<{ id: string; label: string; detected: boolean; registered: boolean; config_path: string; vault: string | null }>;
  connections: Array<{ client: "claude" | "codex" | "antigravity" | "unknown"; pid: number; started_at: string | null; vault: string | null }>;
  error: string | null;
}

interface SyncInfo {
  configured: boolean;
  remote: string | null;
  machine: string | null;
  sync_id: string | null;
  last_success: string | null;
  last_attempt: string | null;
  git: "ok" | "error" | "blocked" | "never";
  index: "ok" | "error" | "skipped" | "never";
  last_error: string | null;
  blocked_reason: string | null;
  conflicts: number;
  held_secrets: number;
  scheduler: "installed" | "absent";
  interval_seconds: number | null;
  peers: string[];
  error: string | null;
}

interface Snapshot {
  status: StatusInfo;
  clients: ClientsInfo;
  sync: SyncInfo;
  refreshed_at: string;
}

interface CommandResult {
  ok: boolean;
  message: string;
  output?: string;
}

const emptyStatus = (error: string): StatusInfo => ({ version: "unknown", vault: "", vault_exists: false, indexed: false, notes: null, passages: null, embedded: null, semantic: "unknown", state_dir: "", error });
const emptyClients = (error: string): ClientsInfo => ({ clients: [], connections: [], error });
const emptySync = (error: string): SyncInfo => ({ configured: false, remote: null, machine: null, sync_id: null, last_success: null, last_attempt: null, git: "never", index: "never", last_error: null, blocked_reason: null, conflicts: 0, held_secrets: 0, scheduler: "absent", interval_seconds: null, peers: [], error });

let tray: Tray | null = null;
let popover: BrowserWindow | null = null;
let settingsWindow: BrowserWindow | null = null;
let binderyPath: string | null = null;
let selectedVault: string | null = null;
let lastSnapshot: Snapshot | null = null;

function userDataPath(name: string): string {
  return path.join(app.getPath("userData"), name);
}

function appUrl(window: BrowserWindow): string {
  return app.isPackaged ? `file://${path.join(app.getAppPath(), "dist", "index.html")}` : "http://localhost:5173";
}

function assetPath(name: string): string {
  return path.join(app.getAppPath(), "build-assets", name);
}

function safeNativeImage(name: string): Electron.NativeImage {
  try {
    const image = nativeImage.createFromPath(assetPath(name));
    return image.isEmpty() ? nativeImage.createEmpty() : image;
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
  const saved = await readSavedBinderyPath(userDataPath("bindery-path.json"));
  const resolved = saved ?? await findExecutable(binderyCandidates());
  if (resolved) {
    binderyPath = resolved;
    await saveBinderyPath(userDataPath("bindery-path.json"), resolved);
  }
  return resolved;
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

async function binderyJson<T>(args: string[], fallback: T): Promise<T> {
  const executable = await ensureBinderyPath();
  if (!executable) return fallback;
  const result = await runJsonCommand<T>(executable, args);
  if (result.value) return result.value;
  return fallback;
}

async function getStatus(): Promise<StatusInfo> {
  const fallback = emptyStatus("Bindery status unavailable");
  return binderyJson<StatusInfo>(["status", "--json"], fallback);
}

async function getClients(): Promise<ClientsInfo> {
  return binderyJson<ClientsInfo>(["clients", "--json"], emptyClients("Bindery clients unavailable"));
}

async function getSync(): Promise<SyncInfo> {
  return binderyJson<SyncInfo>(["sync", "status", "--json"], emptySync("Bindery sync status unavailable"));
}

async function getSnapshot(): Promise<Snapshot> {
  const [status, clients, sync] = await Promise.all([getStatus(), getClients(), getSync()]);
  const snapshot = { status, clients, sync, refreshed_at: new Date().toISOString() };
  lastSnapshot = snapshot;
  updateTray(snapshot);
  return snapshot;
}

function updateTray(snapshot: Snapshot | null): void {
  if (!tray) return;
  const active = Boolean(snapshot?.clients.connections.length || snapshot?.sync.git === "ok");
  const name = active ? "trayActiveTemplate.png" : "trayTemplate.png";
  tray.setImage(safeNativeImage(name));
}

function rendererUrl(window: BrowserWindow): string {
  return appUrl(window);
}

function configureWindow(window: BrowserWindow): void {
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.on("will-redirect", (event) => event.preventDefault());
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
}

async function loadWindow(window: BrowserWindow): Promise<void> {
  if (app.isPackaged) await window.loadFile(path.join(app.getAppPath(), "dist", "index.html"));
  else await window.loadURL(rendererUrl(window));
}

function createPopover(): BrowserWindow {
  const window = new BrowserWindow({
    width: 360,
    height: 650,
    show: false,
    frame: false,
    resizable: false,
    movable: false,
    skipTaskbar: true,
    transparent: false,
    backgroundColor: "#000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  configureWindow(window);
  window.on("blur", () => { if (window.isVisible()) window.hide(); });
  void loadWindow(window);
  return window;
}

function showPopover(): void {
  if (!popover || !tray) return;
  const trayBounds = tray.getBounds();
  const windowBounds = popover.getBounds();
  const x = Math.round(trayBounds.x + trayBounds.width / 2 - windowBounds.width / 2);
  const y = Math.round(trayBounds.y + trayBounds.height + 6);
  popover.setPosition(x, y, false);
  popover.show();
  popover.focus();
  void getSnapshot();
}

function createSettingsWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 760,
    height: 720,
    minWidth: 640,
    minHeight: 620,
    title: "Bindery",
    show: false,
    backgroundColor: "#000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      additionalArguments: ["--bindery-window=settings"],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  configureWindow(window);
  window.on("closed", () => { settingsWindow = null; });
  void loadWindow(window);
  return window;
}

function showSettings(): void {
  if (!settingsWindow) settingsWindow = createSettingsWindow();
  if (popover?.isVisible()) popover.hide();
  settingsWindow.show();
  settingsWindow.focus();
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
  return app.isPackaged ? path.join(process.resourcesPath, "wheel") : path.resolve(__dirname, "..", "dist-wheel");
}

function registerIpc(): void {
  const guarded = <T>(channel: string, action: (args: unknown) => Promise<T> | T): void => {
    ipcMain.handle(channel, async (event, args: unknown) => {
      validateIpcRequest(channel, args, event.senderFrame?.url);
      return action(args);
    });
  };

  guarded(IPC_CHANNELS.getWindowKind, () => (settingsWindow?.webContents.getURL().includes("settings") ? "settings" : process.argv.includes("--bindery-window=settings") ? "settings" : "popover"));
  guarded(IPC_CHANNELS.getSnapshot, () => getSnapshot());
  guarded(IPC_CHANNELS.syncNow, async () => {
    await operationResult("sync", ["sync", "--json"]);
    return getSnapshot();
  });
  guarded(IPC_CHANNELS.openVault, async () => {
    const status = await getStatus();
    const target = getConfiguredVault(status);
    try {
      await stat(target);
      return { ok: true, message: await shell.openPath(target) };
    } catch (error) {
      return { ok: false, message: error instanceof Error ? error.message : "Vault could not be opened" };
    }
  });
  guarded(IPC_CHANNELS.openLog, async () => {
    const target = path.join(app.getPath("logs"), "bindery-app.log");
    try {
      await mkdir(path.dirname(target), { recursive: true });
      return { ok: true, message: await shell.openPath(target) };
    } catch (error) {
      return { ok: false, message: error instanceof Error ? error.message : "Log could not be opened" };
    }
  });
  guarded(IPC_CHANNELS.showSettings, () => { showSettings(); });
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
    const parent = settingsWindow ?? popover;
    const result = parent
      ? await dialog.showOpenDialog(parent, { properties: ["openDirectory", "createDirectory"] })
      : await dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] });
    const chosen = result.canceled ? null : result.filePaths[0] ?? null;
    if (chosen) { assertAbsolutePath(chosen); selectedVault = path.resolve(chosen); }
    return selectedVault;
  });
  guarded(IPC_CHANNELS.installClients, () => installClients());
  guarded(IPC_CHANNELS.writeGlobalPrompt, () => operationResult("prompt", ["prompt", "--global", "--write"]));
  guarded(IPC_CHANNELS.initSync, async (args) => {
    const vault = getConfiguredVault(await getStatus());
    const remote = args as string;
    return operationResult("sync init", ["sync", "init", "--remote", remote, "--vault", vault, "--write"]);
  });
  guarded(IPC_CHANNELS.pairSync, async (args) => {
    const vault = getConfiguredVault(await getStatus());
    const remote = args as string;
    return operationResult("sync pair", ["sync", "pair", remote, "--vault", vault]);
  });
  guarded(IPC_CHANNELS.installScheduler, async () => {
    const vault = getConfiguredVault(await getStatus());
    return operationResult("sync install", ["sync", "install", "--vault", vault, "--write"]);
  });
}

function createTray(): void {
  tray = new Tray(safeNativeImage("trayTemplate.png"));
  tray.setToolTip("Bindery");
  tray.on("click", () => {
    if (popover?.isVisible()) popover.hide();
    else showPopover();
  });
}

app.whenReady().then(() => {
  configureRendererTrust({ packaged: app.isPackaged, indexPath: path.join(app.getAppPath(), "dist", "index.html") });
  if (process.platform === "darwin") app.dock?.hide();
  registerIpc();
  popover = createPopover();
  createTray();
  void getSnapshot();
});

app.on("window-all-closed", () => {
  // Bindery is a menu bar app; closing a settings window must not quit the tray process.
});
app.on("before-quit", () => { tray?.destroy(); });

