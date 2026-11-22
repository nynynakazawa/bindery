import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { IndexView } from "./IndexView";

it("reserves at least 220px for the title and allows long paths to be ellipsized", () => {
  const path = "long/directory/names/for/indexed/notes/note.md";
  const output = renderToStaticMarkup(<IndexView open={() => {}} source={{
    data: {
      notes: [{ path, title: "A note with a long title", project: "", tags: [], tier: "durable", mtime: 0,
        chunks: 1, uses: 0, impressions: 0, last_used: 0 }],
      totals: { notes: 1, chunks: 1, vectors: 0, links: 0, projects: { "": 1 } },
    },
    loading: false, refresh: async () => {},
  }} />);

  const cols = /<colgroup>(.*?)<\/colgroup>/u.exec(output)?.[1] ?? "";
  expect(cols).toMatch(/^<col\/><col style="width:\d+px"\/>/u);
  const fixedWidths = Array.from(cols.matchAll(/width:(\d+)px/gu), ([, width]) => Number(width));
  const minWidth = Number(/min-width:(\d+)px;table-layout:fixed/u.exec(output)?.[1]);
  const titleWidth = minWidth - fixedWidths.reduce((sum, width) => sum + width, 0);
  expect(fixedWidths).toHaveLength(7);
  expect(titleWidth).toBeGreaterThanOrEqual(220);
  expect(titleWidth).toBeGreaterThan(Math.max(...fixedWidths));
  expect(output).toContain(`<span class="mono ellipsis" title="${path}">${path}</span>`);
});
