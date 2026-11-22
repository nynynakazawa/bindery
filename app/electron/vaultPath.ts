import { realpath, stat } from "node:fs/promises";
import path from "node:path";

export function assertRelativePath(value: unknown): asserts value is string {
  if (typeof value !== "string" || !value || value.length > 2048 ||
      value.includes("\0") || value.includes("\\") || path.posix.isAbsolute(value) ||
      /^[a-z]:/iu.test(value) || value.split("/").includes("..")) {
    throw new Error("Invalid Vault-relative path");
  }
}

export async function resolveVaultFile(vault: string, relative: string): Promise<string> {
  assertRelativePath(relative);
  const root = await realpath(vault);
  const target = await realpath(path.resolve(root, relative));
  const within = path.relative(root, target);
  if (!within || within === ".." || within.startsWith(`..${path.sep}`) || path.isAbsolute(within) ||
      !(await stat(target)).isFile()) {
    throw new Error("File is outside the Vault or is not a file");
  }
  return target;
}
