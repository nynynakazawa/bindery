import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { findNoteHeading, renderNoteMarkdown } from "./markdown";

const render = (source: string) => renderToStaticMarkup(<>{renderNoteMarkdown(source, "note.md", ["other.md"], () => {})}</>);

describe("Markdown viewer", () => {
  it("escapes raw HTML and never emits a remotely loaded image", () => {
    const output = render("<script>alert(1)</script>\n\n![a < b](https://example.com/a.png)\n\n![[local.png]]\n\n<img src='https://example.com/b.png'>");
    expect(output).toContain("&lt;script&gt;");
    expect(output).toContain("&lt;img");
    expect(output).toContain("a &lt; b");
    expect(output).not.toMatch(/<(script|img)\b|\bsrc="https?:/u);
  });

  it("renders wiki aliases and explicit safe links without creating navigable URLs", () => {
    const output = render("[[other#Heading|Alias]] [relative](other.md) [web](https://example.com) [unsupported](obsidian://open) https://example.net\n\n`[[code]]`");
    expect(output.match(/class="md-link md-link-note"/gu)).toHaveLength(2);
    expect(output.match(/class="md-link md-link-external"/gu)).toHaveLength(1);
    expect(output).toMatch(/>Alias<\/button>/u);
    expect(output).toContain("<span>unsupported</span>");
    expect(output).toContain("<code>[[code]]</code>");
    expect(output).not.toContain("href=");
  });

  it("renders headings, tables, task lists and code as readable text", () => {
    const output = render("## 設定\n\n- [x] Task\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```sh\necho '<img>'\n```");
    expect(output).toContain('<h2 data-heading="設定">設定</h2>');
    expect(output).toContain("<table>");
    expect(output).toContain('type="checkbox"');
    expect(output).toContain("&lt;img&gt;");
  });

  it("prefers the search breadcrumb when sibling sections share a heading", () => {
    const headings = Array.from(render("# 別の親\n## 子\n# 親\n### 中間\n#### 子\n## 子").matchAll(
      /<(h[1-6]) data-heading="([^"]+)">/gu,
    ), ([, tag, heading]) => ({ tagName: tag.toUpperCase(), dataset: { heading } }));

    expect(findNoteHeading(headings, "親 / 子")).toBe(headings[5]);
    expect(findNoteHeading(headings, "親 / 中間 / 子")).toBe(headings[4]);
    expect(findNoteHeading(headings, "以前の親 / 子")).toBe(headings[1]);
    expect(findNoteHeading(headings, "子")).toBe(headings[1]);
  });
});
