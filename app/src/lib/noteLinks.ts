export interface NoteLocation {
  path: string;
  heading?: string;
}

export type NoteLink =
  | { kind: "note"; location: NoteLocation }
  | { kind: "external"; url: string };

function decoded(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function normalPath(value: string, base: string[]): string | undefined {
  if (value.startsWith("/") || value.includes("\\") || value.includes("\0") || /^[a-z][a-z\d+.-]*:/iu.test(value)) return;
  const parts = [...base];
  for (const part of value.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (!parts.length) return;
      parts.pop();
    } else {
      parts.push(part);
    }
  }
  const path = parts.join("/");
  if (path.length <= 2048) return path;
}

export function resolveNoteLink(href: string, currentPath: string, indexedPaths: string[], wiki = false): NoteLink | undefined {
  if (!wiki && /^https?:\/\//iu.test(href)) {
    try {
      return { kind: "external", url: new URL(href).href };
    } catch {
      return;
    }
  }
  const fragment = href.indexOf("#");
  let target = decoded(fragment < 0 ? href : href.slice(0, fragment)).trim();
  const heading = fragment < 0 ? undefined : decoded(href.slice(fragment + 1)).trim();
  if (!target) return heading ? { kind: "note", location: { path: currentPath, heading } } : undefined;
  if (!/\.md$/iu.test(target)) {
    if (!wiki) return;
    target += ".md";
  }

  const directory = currentPath.split("/").slice(0, -1);
  const sibling = normalPath(target, directory);
  if (!sibling) return;
  if (!wiki) return { kind: "note", location: { path: sibling, heading } };
  if (target.startsWith("./") || target.startsWith("../")) return { kind: "note", location: { path: sibling, heading } };

  const root = normalPath(target, []);
  const candidates = [sibling, root];
  const exact = candidates.find((candidate) => candidate && indexedPaths.includes(candidate));
  if (exact) return { kind: "note", location: { path: exact, heading } };

  const folded = (value: string) => value.normalize("NFC").toLowerCase();
  const caseInsensitive = candidates.map((candidate) => candidate && indexedPaths.find((path) => folded(path) === folded(candidate))).find(Boolean);
  if (caseInsensitive) return { kind: "note", location: { path: caseInsensitive, heading } };

  const suffix = "/" + folded(target);
  const matches = indexedPaths.filter((path) => folded(path).endsWith(suffix));
  // Leave an ambiguous bare name unresolved.
  const fallback = target.includes("/") ? root ?? sibling : sibling;
  return { kind: "note", location: { path: matches.length === 1 ? matches[0] : fallback, heading } };
}
