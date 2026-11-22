import { describe, expect, it } from "vitest";
import { resolveNoteLink } from "./noteLinks";

const paths = ["daily/notes.md", "work/notes.md", "guides/認証.md", "guides/API.md"];

describe("note links", () => {
  it("prefers the current directory for a wikilink and keeps the heading", () => {
    expect(resolveNoteLink("notes#設定", "work/today.md", paths, true))
      .toEqual({ kind: "note", location: { path: "work/notes.md", heading: "設定" } });
  });

  it("resolves unique filenames, Vault-relative paths and Unicode", () => {
    expect(resolveNoteLink("api", "daily/today.md", paths, true)).toMatchObject({ location: { path: "guides/API.md" } });
    expect(resolveNoteLink("guides/%E8%AA%8D%E8%A8%BC", "daily/today.md", paths, true)).toMatchObject({ location: { path: "guides/認証.md" } });
    expect(resolveNoteLink("#今日", "daily/today.md", paths, true)).toMatchObject({ location: { path: "daily/today.md", heading: "今日" } });
  });

  it("keeps ambiguous names local and preserves a missing destination for the viewer", () => {
    expect(resolveNoteLink("notes", "elsewhere/today.md", paths, true)).toMatchObject({ location: { path: "elsewhere/notes.md" } });
    expect(resolveNoteLink("missing/file", "daily/today.md", paths, true)).toMatchObject({ location: { path: "missing/file.md" } });
  });

  it("normalizes relative Markdown links inside the Vault and ignores escapes or other schemes", () => {
    expect(resolveNoteLink("../guides/API.md#use", "daily/today.md", paths)).toEqual({ kind: "note", location: { path: "guides/API.md", heading: "use" } });
    expect(resolveNoteLink("https://example.com", "daily/today.md", paths)).toEqual({ kind: "external", url: "https://example.com/" });
    for (const link of ["../../outside.md", "%2e%2e/%2e%2e/out.md", "/etc/hosts.md", "obsidian://open", "javascript:alert(1)", "file:///tmp/example.md", "//example.org/a.md", "guide.pdf"]) {
      expect(resolveNoteLink(link, "daily/today.md", paths)).toBeUndefined();
    }
  });
});
