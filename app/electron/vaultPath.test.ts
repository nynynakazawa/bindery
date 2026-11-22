import { mkdtemp, mkdir, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { assertRelativePath, resolveVaultFile } from "./vaultPath";

describe("Vault-relative path validation", () => {
  it.each(["", "/a.md", "C:/a.md", "C:a.md", "../a.md", "notes/../a.md", "notes\\..\\a.md", "nul\0.md", "a".repeat(2049)])("rejects %j", (input) => {
    expect(() => assertRelativePath(input)).toThrow();
  });
  it("accepts paths of at most 2048 characters and ordinary dots within a filename", () => {
    expect(() => assertRelativePath("a".repeat(2048))).not.toThrow();
    expect(() => assertRelativePath("notes/あ..b.md")).not.toThrow();
  });
});

describe("Vault file resolution", () => {
  let temp: string;
  let vault: string;

  beforeEach(async () => {
    temp = await realpath(await mkdtemp(path.join(tmpdir(), "bindery-vault-path-")));
    vault = path.join(temp, "vault");
    await mkdir(vault);
    await mkdir(path.join(temp, "vault-other"));
    await Promise.all([
      writeFile(path.join(vault, "note.md"), "# Note"),
      writeFile(path.join(temp, "vault-other", "outside.md"), "# Outside"),
    ]);
  });

  afterEach(async () => {
    if (temp) await rm(temp, { recursive: true, force: true });
  });

  it("returns real paths for files and for Vaults reached via symlink", async () => {
    const alias = path.join(temp, "alias");
    await symlink(vault, alias);
    await symlink(path.join(vault, "note.md"), path.join(vault, "inside.md"));
    await expect(resolveVaultFile(alias, "inside.md")).resolves.toBe(path.join(vault, "note.md"));
  });

  it("rejects symlink escapes, including directories sharing the Vault prefix", async () => {
    await symlink(path.join(temp, "vault-other"), path.join(vault, "linked"));
    await expect(resolveVaultFile(vault, "linked/outside.md")).rejects.toThrow("outside the Vault");
    await expect(resolveVaultFile(vault, "../vault-other/outside.md")).rejects.toThrow("Vault-relative");
    await expect(resolveVaultFile(vault, "missing.md")).rejects.toThrow();
    await expect(resolveVaultFile(vault, ".")).rejects.toThrow();
  });
});
