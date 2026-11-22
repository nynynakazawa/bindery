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
