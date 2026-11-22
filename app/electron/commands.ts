import { access, constants, readFile, readdir, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const execFileAsync = promisify(execFile);
const COMMAND_TIMEOUT_MS = 15_000;
const MAX_OUTPUT_BYTES = 512 * 1024;

export interface CommandOutput {
  stdout: string;
  stderr: string;
  code: number;
}

export interface JsonCommandResult<T> {
  value: T | null;
  output: CommandOutput;
  error: string | null;
}

function isAbsoluteExecutable(candidate: string): boolean {
  return path.isAbsolute(candidate) && !/[\u0000\r\n]/u.test(candidate);
}

export async function executableExists(candidate: string): Promise<boolean> {
  if (!isAbsoluteExecutable(candidate)) return false;
  try {
    await access(candidate, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export async function findExecutable(candidates: string[]): Promise<string | null> {
  for (const candidate of candidates) {
    if (await executableExists(candidate)) return path.resolve(candidate);
  }
  return null;
}

export async function runAbsoluteCommand(executable: string, args: string[], timeout = COMMAND_TIMEOUT_MS, maxBuffer = MAX_OUTPUT_BYTES): Promise<CommandOutput> {
  if (!isAbsoluteExecutable(executable)) throw new Error("Executable must be an absolute path");
  if (args.some((arg) => typeof arg !== "string" || /[\u0000]/u.test(arg))) throw new Error("Invalid command argument");
  try {
    const result = await execFileAsync(executable, args, {
      shell: false,
      windowsHide: true,
      timeout,
      maxBuffer,
      encoding: "utf8",
    });
    return { stdout: result.stdout, stderr: result.stderr, code: 0 };
  } catch (error) {
    const failure = error as NodeJS.ErrnoException & { stdout?: string; stderr?: string; code?: number | string; killed?: boolean };
    const code = typeof failure.code === "number" ? failure.code : failure.killed ? 124 : 1;
    return { stdout: failure.stdout ?? "", stderr: failure.stderr ?? "", code };
  }
}

export function parseJson<T>(stdout: string): T {
  const trimmed = stdout.trim();
  if (!trimmed) throw new Error("Bindery returned no JSON");
  return JSON.parse(trimmed) as T;
}

export async function runJsonCommand<T>(executable: string, args: string[], options: { maxBuffer?: number; timeout?: number } = {}): Promise<JsonCommandResult<T>> {
  const output = await runAbsoluteCommand(executable, args, options.timeout, options.maxBuffer);
  try {
    return { value: parseJson<T>(output.stdout), output, error: null };
  } catch (error) {
    return { value: null, output, error: error instanceof Error ? error.message : "Invalid JSON" };
  }
}

export function binderyCandidates(home = os.homedir()): string[] {
  return [
    path.join(home, ".local", "bin", "bindery"),
    path.join(home, ".cargo", "bin", "bindery"),
    "/opt/homebrew/bin/bindery",
    "/usr/local/bin/bindery",
    "/usr/bin/bindery",
  ];
}

export function uvCandidates(home = os.homedir()): string[] {
  return [
    path.join(home, ".local", "bin", "uv"),
    path.join(home, ".cargo", "bin", "uv"),
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    "/usr/bin/uv",
  ];
}

export async function readSavedBinderyPath(filePath: string): Promise<string | null> {
  try {
    const content = JSON.parse(await readFile(filePath, "utf8")) as { path?: unknown };
    if (typeof content.path !== "string" || !isAbsoluteExecutable(content.path)) return null;
    return (await executableExists(content.path)) ? path.resolve(content.path) : null;
  } catch {
    return null;
  }
}

export async function saveBinderyPath(filePath: string, executable: string): Promise<void> {
  if (!isAbsoluteExecutable(executable)) throw new Error("Cannot save a non-absolute executable");
  await writeFile(filePath, `${JSON.stringify({ path: path.resolve(executable) })}\n`, { mode: 0o600 });
}

export async function findWheel(directory: string): Promise<string | null> {
  try {
    const names = await readdir(directory);
    const wheel = names.filter((name) => name.endsWith(".whl")).sort()[0];
    return wheel ? path.join(directory, wheel) : null;
  } catch {
    return null;
  }
}
