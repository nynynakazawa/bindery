import path from "node:path";

import { IPC_CHANNELS } from "./channels";
import { assertRelativePath } from "./vaultPath";

export { IPC_CHANNELS } from "./channels";

export interface RendererTrust {
  /** Packaged builds trust exactly one file: the bundled renderer. */
  packaged: boolean;
  /** Absolute path of the bundled dist/index.html. */
  indexPath: string;
}

// Until main configures it, nothing is trusted: a sender checked before the
// window exists has no legitimate reason to talk to the main process.
let trust: RendererTrust | null = null;

export function configureRendererTrust(value: RendererTrust): void {
  trust = value;
}

export function isTrustedRendererUrl(rawUrl: string, current: RendererTrust | null = trust): boolean {
  if (!current) return false;
  try {
    const url = new URL(rawUrl);
    if (url.protocol === "file:" && current.packaged && !url.hostname) {
      // Compare the whole path, not a suffix: any other file named
      // dist/index.html on disk must not be able to drive the app.
      return path.resolve(decodeURIComponent(url.pathname)) === path.resolve(current.indexPath);
    }
    // The Vite dev server exists only in unpackaged development builds.
    if (current.packaged) return false;
    return url.protocol === "http:" &&
      (url.hostname === "localhost" || url.hostname === "127.0.0.1") &&
      url.port === "5173";
  } catch {
    return false;
  }
}

export function assertTrustedSender(senderUrl: string | undefined): void {
  if (!senderUrl || !isTrustedRendererUrl(senderUrl)) {
    throw new Error("Untrusted renderer sender");
  }
}

function assertNoArguments(args: unknown): void {
  if (args !== undefined) throw new Error("This IPC operation does not accept arguments");
}

function assertRemote(args: unknown): asserts args is string {
  if (typeof args !== "string" || args.length === 0 || args.length > 512 || /[\u0000\r\n]/u.test(args)) {
    throw new Error("Invalid remote");
  }
}

export function assertAbsolutePath(value: string): void {
  if (!path.isAbsolute(value) || value.length > 4096 || /[\u0000\r\n]/u.test(value)) {
    throw new Error("Invalid absolute path");
  }
}

/**
 * Every exposed IPC channel is checked here before its dedicated handler runs.
 * There is intentionally no command or argument channel in this allow-list.
 */
export function validateIpcRequest(channel: string, args: unknown, senderUrl: string | undefined): void {
  assertTrustedSender(senderUrl);
  switch (channel) {
    case IPC_CHANNELS.getSnapshot:
    case IPC_CHANNELS.syncNow:
    case IPC_CHANNELS.openVault:
    case IPC_CHANNELS.openLog:
    case IPC_CHANNELS.checkUv:
    case IPC_CHANNELS.installWheel:
    case IPC_CHANNELS.selectVault:
    case IPC_CHANNELS.installClients:
    case IPC_CHANNELS.writeGlobalPrompt:
    case IPC_CHANNELS.installScheduler:
    case IPC_CHANNELS.listNotes:
    case IPC_CHANNELS.recent:
    case IPC_CHANNELS.ranking:
    case IPC_CHANNELS.getLoginItem:
      assertNoArguments(args);
      return;
    case IPC_CHANNELS.initSync:
    case IPC_CHANNELS.pairSync:
      assertRemote(args);
      return;
    case IPC_CHANNELS.search:
      if (typeof args !== "string" || args.length > 500 || args.includes("\0")) throw new Error("Invalid search query");
      return;
    case IPC_CHANNELS.openInObsidian:
      assertRelativePath(args);
      if (!/\.md$/iu.test(args)) throw new Error("Expected a Markdown note");
      return;
    case IPC_CHANNELS.readNote:
    case IPC_CHANNELS.revealInFinder:
      assertRelativePath(args);
      return;
    case IPC_CHANNELS.openExternal:
      parseExternalUrl(args);
      return;
    case IPC_CHANNELS.setLoginItem:
      if (typeof args !== "boolean") throw new Error("Expected a boolean");
      return;
    default:
      throw new Error("Unknown IPC operation");
  }
}

export function parseExternalUrl(value: unknown): URL {
  if (typeof value !== "string" || value.length > 8192 || /[\u0000-\u001f\u007f]/u.test(value) ||
      !/^https?:\/\//iu.test(value)) throw new Error("Invalid external URL");
  const url = new URL(value);
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error("Invalid external URL");
  return url;
}
