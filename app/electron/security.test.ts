import { beforeEach, describe, expect, it } from "vitest";
import { IPC_CHANNELS, configureRendererTrust, isTrustedRendererUrl, validateIpcRequest } from "./security";

const indexPath = "/Applications/Bindery.app/Contents/Resources/app/dist/index.html";
const trusted = `file://${indexPath}`;

beforeEach(() => configureRendererTrust({ packaged: true, indexPath }));

describe("Electron IPC boundary", () => {
  it("accepts only the bundled renderer in a packaged build", () => {
    expect(isTrustedRendererUrl(trusted)).toBe(true);
    expect(isTrustedRendererUrl("http://localhost:5173/")).toBe(false);
    expect(isTrustedRendererUrl("https://example.com/steal")).toBe(false);
    expect(isTrustedRendererUrl("file:///tmp/attacker.html")).toBe(false);
    expect(isTrustedRendererUrl("file:///tmp/evil/dist/index.html")).toBe(false);
  });

  it("trusts the Vite dev server only in development builds", () => {
    expect(isTrustedRendererUrl("http://localhost:5173/", { packaged: false, indexPath })).toBe(true);
    expect(isTrustedRendererUrl("http://localhost:5174/", { packaged: false, indexPath })).toBe(false);
  });

  it("trusts nothing before the main process configures it", () => {
    expect(isTrustedRendererUrl(trusted, null)).toBe(false);
  });

  it("rejects untrusted senders and malformed remote arguments", () => {
    expect(() => validateIpcRequest(IPC_CHANNELS.getSnapshot, undefined, "https://evil.example/"))
      .toThrow("Untrusted renderer sender");
    expect(() => validateIpcRequest(IPC_CHANNELS.initSync, "remote\n--write", trusted)).toThrow("Invalid remote");
    expect(() => validateIpcRequest(IPC_CHANNELS.initSync, ["remote"], trusted)).toThrow("Invalid remote");
    expect(() => validateIpcRequest(IPC_CHANNELS.getSnapshot, "unexpected", trusted)).toThrow("does not accept arguments");
  });

  it("does not expose a generic command execution channel", () => {
    expect(Object.values(IPC_CHANNELS)).not.toContain("app:run-command");
    expect(() => validateIpcRequest("app:run-command", ["rm", "-rf", "/"], trusted)).toThrow("Unknown IPC operation");
  });
});

describe("dashboard IPC arguments", () => {
  const noArgs = [IPC_CHANNELS.listNotes, IPC_CHANNELS.recent, IPC_CHANNELS.ranking, IPC_CHANNELS.getLoginItem];
  const notePaths = [IPC_CHANNELS.readNote, IPC_CHANNELS.openInObsidian, IPC_CHANNELS.revealInFinder];
  const valid = [
    ...noArgs.map((channel) => [channel, undefined] as const),
    ...notePaths.map((channel) => [channel, "folder/日本語.md"] as const),
    [IPC_CHANNELS.search, "a".repeat(500)],
    [IPC_CHANNELS.openExternal, "https://example.com/path"],
    [IPC_CHANNELS.setLoginItem, true],
  ] as const;

  it.each(valid)("validates the sender for %s", (channel, arg) => {
    expect(() => validateIpcRequest(channel, arg, trusted)).not.toThrow();
    expect(() => validateIpcRequest(channel, arg, undefined)).toThrow("Untrusted renderer sender");
    expect(() => validateIpcRequest(channel, arg, "http://localhost:5173")).toThrow("Untrusted renderer sender");
  });

  it("rejects extra payloads on no-argument operations", () => {
    for (const channel of noArgs) {
      for (const arg of [null, [], false, {}, ""]) {
        expect(() => validateIpcRequest(channel, arg, trusted)).toThrow();
      }
    }
  });

  it("rejects malformed inputs to search, paths, browser opening and login settings", () => {
    for (const channel of notePaths) {
      for (const arg of [null, [], 0, "/root.md", "a/../b.md", "a\0b.md", `${"a".repeat(2046)}.md`]) {
        expect(() => validateIpcRequest(channel, arg, trusted)).toThrow();
      }
    }
    expect(() => validateIpcRequest(IPC_CHANNELS.readNote, "drafts/local.env", trusted)).not.toThrow();
    expect(() => validateIpcRequest(IPC_CHANNELS.revealInFinder, "drafts/local.env", trusted)).not.toThrow();
    expect(() => validateIpcRequest(IPC_CHANNELS.openInObsidian, "drafts/local.env", trusted)).toThrow();
    for (const arg of [0, [], "a\0b", "a".repeat(501)]) {
      expect(() => validateIpcRequest(IPC_CHANNELS.search, arg, trusted)).toThrow();
    }
    for (const arg of ["file:///etc/hosts", "obsidian://open", "mailto:test@example.com", "//example.com", "https://", "https://exam\nple.com", []]) {
      expect(() => validateIpcRequest(IPC_CHANNELS.openExternal, arg, trusted)).toThrow();
    }
    for (const arg of [undefined, "false", 1, { enabled: true }]) {
      expect(() => validateIpcRequest(IPC_CHANNELS.setLoginItem, arg, trusted)).toThrow();
    }
  });

  it("does not trust a file URL with a remote authority", () => {
    expect(isTrustedRendererUrl(`file://example.com${indexPath}`)).toBe(false);
  });
});
