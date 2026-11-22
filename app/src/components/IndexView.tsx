import { useMemo, useState, type ReactNode } from "react";
import { Button, Checkbox, Chip, MenuItem, Select, Skeleton, Table, TableBody, TableCell, TableHead, TableRow, TextField } from "@mui/material";
import ArrowDownwardOutlined from "@mui/icons-material/ArrowDownwardOutlined";
import ArrowUpwardOutlined from "@mui/icons-material/ArrowUpwardOutlined";
import { t, type CopyKey } from "../copy";
import type { ListState } from "../lib/dashboardData";
import type { NoteLocation } from "../lib/noteLinks";
import type { NoteSummary, NotesData } from "../types";
import { dateTime, Empty, LoadingRows, number, Problem, TagList, unavailable } from "./DashboardParts";

type Sortable = "title" | "path" | "project" | "tier" | "mtime" | "chunks" | "uses";
type Column = { key: Sortable | "tags"; label: CopyKey; width: number; numeric?: boolean };
const columns: Column[] = [
  { key: "title", label: "index.title", width: 220 },
  { key: "path", label: "index.path", width: 116 },
  { key: "project", label: "index.project", width: 96 },
  { key: "tags", label: "index.tags", width: 110 },
  { key: "tier", label: "index.tier", width: 68 },
  { key: "mtime", label: "index.updated", width: 88, numeric: true },
  { key: "chunks", label: "index.chunks", width: 56, numeric: true },
  { key: "uses", label: "index.uses", width: 56, numeric: true },
];
const tableMinWidth = columns.reduce((width, column) => width + column.width, 0);
const PAGE_SIZE = 100;
const emptyNotes: NoteSummary[] = [];
const fileName = (path: string) => path.split("/").at(-1) ?? path;

function modified(note: NoteSummary): number {
  const time = note.mtime;
  if (typeof time === "number") return Math.abs(time) < 1e12 ? time * 1000 : time;
  return time ? Date.parse(time) || 0 : 0;
}

function sortValue(note: NoteSummary, key: Sortable) {
  if (key === "mtime") return modified(note);
  if (key === "title") return note.title || fileName(note.path);
  return note[key] ?? "";
}

export function IndexView({ source, activePath, open }: {
  source: ListState<NotesData>; activePath?: string; open: (note: NoteLocation) => void;
}) {
  const [project, setProject] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [sort, setSort] = useState<{ key: Sortable; direction: "asc" | "desc" }>({ key: "mtime", direction: "desc" });
  const notes = source.data?.notes ?? emptyNotes;
  const totals = source.data?.totals;
  const tagCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const note of notes) for (const tag of new Set(note.tags)) counts.set(tag, (counts.get(tag) ?? 0) + 1);
    for (const tag of tags) if (!counts.has(tag)) counts.set(tag, 0);
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [notes, tags]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    const factor = sort.direction === "asc" ? 1 : -1;
    return notes.filter((note) => (project === null || note.project === project) &&
      (!needle || note.title.toLocaleLowerCase().includes(needle) || note.path.toLocaleLowerCase().includes(needle)) &&
      tags.every((tag) => note.tags.includes(tag))).sort((a, b) => {
      const first = sortValue(a, sort.key);
      const second = sortValue(b, sort.key);
      const order = typeof first === "number" && typeof second === "number" ? first - second : String(first).localeCompare(String(second), undefined, { numeric: true });
      return order * factor || a.path.localeCompare(b.path);
    });
  }, [notes, project, query, tags, sort]);

  function clear() {
    setProject(null);
    setQuery("");
    setTags([]);
    setLimit(PAGE_SIZE);
  }

  function changeSort(key: Sortable) {
    setSort((current) => ({ key, direction: current.key === key && current.direction === "asc" ? "desc" : "asc" }));
    setLimit(PAGE_SIZE);
  }

  const cell = (note: NoteSummary, key: Column["key"]): ReactNode => {
    switch (key) {
      case "tags": return <TagList tags={note.tags} limit={2} />;
      case "title": return <strong className="ellipsis" title={note.title || fileName(note.path)}>{note.title || fileName(note.path)}</strong>;
      case "path": return <span className="mono ellipsis" title={note.path}>{note.path}</span>;
      case "mtime": return <span className="mono">{dateTime(note.mtime, "short")}</span>;
      case "chunks": return number(note.chunks);
      case "uses": return <span className={note.uses ? "" : "meta"}>{number(note.uses)}</span>;
      case "tier": return note.tier ? <span className="mini-chip">{note.tier}</span> : unavailable;
      case "project": return <span className="ellipsis" title={note.project ?? undefined}>{note.project || unavailable}</span>;
    }
  };

  return <div className="section-stack index-view">
    {source.error && <Problem title={t("overview.indexError")} detail={source.error} retry={() => void source.refresh(true)} />}
    <div className="index-metrics">
      {([
        ["vault.notes", totals?.notes], ["index.chunks", totals?.chunks], ["vault.embedded", totals?.vectors], ["index.links", totals?.links],
      ] as const).map(([label, value]) => <div className="panel index-metric" key={label}>
        <span className="meta">{t(label)}</span>{!totals && source.loading ? <Skeleton width={56} /> : <strong>{number(value)}</strong>}
      </div>)}
    </div>
    {totals && <div className="project-filters">
      <Chip label={`${t("common.all")} (${number(totals.notes)})`} clickable variant={project === null ? "filled" : "outlined"} aria-pressed={project === null} onClick={() => { setProject(null); setLimit(PAGE_SIZE); }} />
      {Object.entries(totals.projects).sort(([a], [b]) => a.localeCompare(b)).map(([name, count]) => <Chip key={name}
        label={`${name || unavailable} (${number(count)})`} clickable variant={project === name ? "filled" : "outlined"} aria-pressed={project === name}
        onClick={() => { setProject(name); setLimit(PAGE_SIZE); }} />)}
    </div>}
    <div className="index-filters">
      <TextField size="small" value={query} placeholder={t("index.filter")} onChange={(event) => { setQuery(event.target.value); setLimit(PAGE_SIZE); }}
        slotProps={{ htmlInput: { "aria-label": t("index.filter") } }} />
      <Select size="small" multiple displayEmpty value={tags} aria-label={t("index.tags")} onChange={(event) => {
        const value = event.target.value;
        setTags(typeof value === "string" ? [value] : value);
        setLimit(PAGE_SIZE);
      }} renderValue={(selected) => selected.length ? t("index.selectedTags", { count: selected.length }) : t("index.tags")}>
        {tagCounts.map(([tag, count]) => <MenuItem key={tag} value={tag}><Checkbox checked={tags.includes(tag)} size="small" />{tag} ({number(count)})</MenuItem>)}
      </Select>
      {(query || project !== null || tags.length > 0) && <Button color="inherit" onClick={clear}>{t("common.clear")}</Button>}
    </div>
    <section className="panel index-table" aria-label={t("nav.index")}>
      <div className="table-scroll">
        <Table size="small" aria-label={t("nav.index")} sx={{ minWidth: tableMinWidth, tableLayout: "fixed" }}>
          <colgroup>{columns.map(({ key, width }) => <col key={key} style={key === "title" ? undefined : { width }} />)}</colgroup>
          <TableHead><TableRow>{columns.map(({ key, label, numeric }) => <TableCell key={key} align={numeric ? "right" : "left"}
            sortDirection={sort.key === key ? sort.direction : false}>
            {key === "tags" ? t(label) : <button type="button" className="table-sort" onClick={() => changeSort(key)}>
              {t(label)}{sort.key === key && (sort.direction === "asc" ? <ArrowUpwardOutlined /> : <ArrowDownwardOutlined />)}
            </button>}
          </TableCell>)}</TableRow></TableHead>
          <TableBody>
            {filtered.slice(0, limit).map((note) => <TableRow key={note.path} hover selected={activePath === note.path} onClick={() => open({ path: note.path })}>
              {columns.map(({ key, numeric }) => <TableCell key={key} align={numeric ? "right" : "left"}>
                {key === "title" ? <button type="button" className="table-note" onClick={(event) => { event.stopPropagation(); open({ path: note.path }); }}>{cell(note, key)}</button> : cell(note, key)}
              </TableCell>)}
            </TableRow>)}
          </TableBody>
        </Table>
      </div>
      {!source.data && source.loading && <LoadingRows count={8} />}
      {source.data && !filtered.length && <Empty>{t(notes.length ? "index.noMatches" : "index.noNotes")}{notes.length > 0 &&
        <Button color="inherit" onClick={clear}>{t("common.clear")}</Button>}</Empty>}
      {Boolean(filtered.length) && <div className="table-footer">
        <span className="meta">{t("common.visibleCount", { shown: number(Math.min(filtered.length, limit)), total: number(filtered.length) })}</span>
        {limit < filtered.length && <Button color="inherit" onClick={() => setLimit(limit + PAGE_SIZE)}>{t("common.more")}</Button>}
      </div>}
    </section>
  </div>;
}
