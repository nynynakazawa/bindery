import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { NoteMetadata } from "./NoteViewer";

it("presents frontmatter tags as individual chips", () => {
  const output = renderToStaticMarkup(<NoteMetadata note={{
    path: "note.md", title: "Note", body: "", mtime: 0, frontmatter: { tags: ["work", "設計"] },
  }} />);

  expect(output).toContain('<span class="tags" title="work, 設計">');
  expect(output).toContain('<span class="mini-chip">work</span>');
  expect(output).toContain('<span class="mini-chip">設計</span>');
});
