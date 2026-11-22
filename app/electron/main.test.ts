import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { IPC_CHANNELS } from "./channels";

type IpcHandler = (event: unknown, ...args: string[]) => Promise<unknown>;

const { handlers, sender, runJsonCommand, reveal } = vi.hoisted(() => ({
  handlers: new Map<string, IpcHandler>(),
  sender: { on: vi.fn(), setWindowOpenHandler: vi.fn() },
  runJsonCommand: vi.fn(),
  reveal: vi.fn(),
}));

const appPath = fileURLToPath(new URL("..", import.meta.url));
const trustedUrl = pathToFileURL(path.join(appPath, "dist", "index.html")).href;
const binary = "/tmp/test-bindery";

vi.mock("electron", () => ({
  app: {
    isPackaged: true, requestSingleInstanceLock: () => true, isReady: () => true,
    whenReady: async () => {}, on: vi.fn(), focus: vi.fn(), getAppPath: () => appPath,
    getPath: (name: string) => {
      if (name === "userData") return "/tmp";
      throw new Error("Logging is disabled in this test");
    },
    getLocale: () => "en",
  },
  BrowserWindow: class {
    webContents = sender;
    on = vi.fn();
    loadFile = vi.fn(async () => {});
    isMinimized = () => false;
    show = vi.fn();
    focus = vi.fn();
  },
  ipcMain: { handle: (channel: string, handler: IpcHandler) => handlers.set(channel, handler) },
  Menu: { setApplicationMenu: vi.fn(), buildFromTemplate: vi.fn() },
  nativeImage: { createFromPath: () => ({ setTemplateImage: vi.fn() }) },
  shell: { showItemInFolder: reveal },
  Tray: class {
    setToolTip = vi.fn();
    setImage = vi.fn();
    on = vi.fn();
  },
}));

vi.mock("./commands", () => ({
  readSavedBinderyPath: async () => binary,
  executableExists: async () => true,
  runJsonCommand,
}));

beforeAll(async () => { await import("./main"); });
beforeEach(() => {
  runJsonCommand.mockReset();
  reveal.mockClear();
});

function invoke(channel: string, ...args: string[]) {
  return handlers.get(channel)!({ sender, senderFrame: { parent: null, url: trustedUrl } }, ...args);
}

it("uses fresh status with held and conflict paths immediately after manual sync", async () => {
  const stale = { configured: true, last_attempt: "2026-11-22T01:00:00Z", error: null };
  const fresh = { ...stale, held_paths: ["drafts/local.env"], conflict_paths: ["note.conflict-000000000000.md"] };
  runJsonCommand.mockImplementation(async (_binary: string, args: string[]) => {
    if (args[0] === "sync" && args[1] === "--json") return { value: { exit_code: 0, status: stale } };
    if (args[0] === "sync" && args[1] === "status") return { value: fresh };
    return { value: { connections: [] } };
  });

  expect(await invoke(IPC_CHANNELS.syncNow)).toMatchObject({ sync: fresh, syncing: false });
  expect(runJsonCommand.mock.calls.slice(0, 2).map(([, args]) => args)).toEqual([
    ["sync", "--json"], ["sync", "status", "--json"],
  ]);
});

it("allows Finder to reveal a non-Markdown review file without invoking the CLI from readNote", async () => {
  runJsonCommand.mockResolvedValue({ value: { vault: appPath, vault_exists: true, connections: [] } });
  await invoke(IPC_CHANNELS.getSnapshot);
  runJsonCommand.mockClear();

  expect(await invoke(IPC_CHANNELS.readNote, "package.json")).toEqual({
    error: "Expected a Markdown note", canReveal: true,
  });
  expect(await invoke(IPC_CHANNELS.readNote, "missing.env")).toEqual({
    error: "Expected a Markdown note", canReveal: false,
  });
  expect(runJsonCommand).not.toHaveBeenCalled();
  expect(await invoke(IPC_CHANNELS.revealInFinder, "package.json")).toBeUndefined();
  expect(reveal).toHaveBeenCalledWith(path.join(appPath, "package.json"));
  expect(runJsonCommand.mock.calls.map(([, args]) => args)).toEqual([["status", "--json"]]);
});
