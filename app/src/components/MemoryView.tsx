import { Button, CircularProgress, IconButton, InputAdornment, TextField } from "@mui/material";
import ClearOutlined from "@mui/icons-material/ClearOutlined";
import SearchOutlined from "@mui/icons-material/SearchOutlined";
import { useRef, useState } from "react";
import { t } from "../copy";
import type { ListState } from "../lib/dashboardData";
import type { NoteLocation } from "../lib/noteLinks";
import { useNoteSearch } from "../lib/search";
import type { RankingData, RecentEntry } from "../types";
import { Empty, LoadingRows, NoteRow, number, Panel, Problem, TagList } from "./DashboardParts";

type RankRow = { key: string; label: string; value: number; detail?: string; action: () => void };

function RankPanel({ title, rows, source }: { title: string; rows: RankRow[]; source: ListState<RankingData> }) {
  const [expanded, setExpanded] = useState(false);
  const more = Math.min(rows.length - 5, 5);
  return <Panel title={title} action={<span className="meta">{t("memory.allTime")}</span>}>
    {source.error && <Problem title={t("memory.rankingError")} retry={() => void source.refresh(true)} />}
    {!source.data ? source.loading && <LoadingRows count={5} /> : rows.length === 0 ? <Empty>{t("memory.noRecords")}</Empty> :
      rows.slice(0, expanded ? 10 : 5).map((row, index) => <NoteRow key={row.key} title={row.label} leading={index + 1}
        trailing={t("common.times", { count: number(row.value) })} onClick={row.action}>
        {row.detail && <span className="rank-detail meta">{row.detail}</span>}
        <span className="rank-bar" aria-hidden="true"><span style={{ width: `${100 * row.value / Math.max(rows[0].value, 1)}%` }} /></span>
      </NoteRow>)}
    {more > 0 && <Button color="inherit" size="small" className="rank-more" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
      {expanded ? t("common.less") : t("common.moreCount", { count: more })}
    </Button>}
  </Panel>;
}

function highlighted(snippet: string, query: string) {
  const words = [...new Set(query.split(/\s+/u).filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!words.length) return snippet;
  const escaped = words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"));
  return snippet.split(new RegExp(`(${escaped.join("|")})`, "giu")).map((part, index) => index % 2 ? <mark key={index}>{part}</mark> : part);
}

function matchLabel(match: string) {
  if (match === "keyword") return t("memory.match.keyword");
  if (match === "semantic") return t("memory.match.semantic");
  if (match === "both" || match === "hybrid" || (match.includes("keyword") && match.includes("semantic"))) return t("memory.match.both");
  return match;
}

export function MemoryView({ rankings, recent, open }: {
  rankings: ListState<RankingData>; recent: ListState<{ entries: RecentEntry[] }>; open: (note: NoteLocation) => void;
}) {
  const [query, setQuery] = useState("");
  const { state, run } = useNoteSearch(window.bindery.search);
  const input = useRef<HTMLInputElement>(null);

  function change(value: string, immediate = false) {
    setQuery(value);
    run(value.trim(), immediate ? 0 : 250);
  }

  return <div className="section-stack">
    <div className="memory-search">
      <TextField fullWidth value={query} inputRef={input} placeholder={t("memory.search")} onChange={(event) => change(event.target.value)}
        slotProps={{
          htmlInput: { "aria-label": t("memory.search"), id: "memory-search-input", maxLength: 500 },
          input: {
            startAdornment: <InputAdornment position="start"><SearchOutlined /></InputAdornment>,
            endAdornment: <InputAdornment position="end">
              {state.kind === "loading" && <CircularProgress size={16} />}
              {query ? <IconButton size="small" aria-label={t("memory.clearSearch")} onClick={() => { change(""); input.current?.focus(); }}><ClearOutlined /></IconButton> : <kbd>⌘K</kbd>}
            </InputAdornment>,
          },
        }} />
    </div>
    {query.trim() && <Panel title={t("memory.searchResults")}>
      {state.kind === "loading" && <LoadingRows />}
      {state.kind === "error" && <Problem title={t("memory.searchError")} detail={state.message} retry={() => run(query.trim(), 0)} />}
      {state.kind === "ready" && (state.results.length === 0 ? <Empty>{t("memory.noMatches", { query: query.trim() })}</Empty> :
        state.results.map((entry, index) => <button type="button" key={`${entry.path}:${index}`} className="search-result"
          onClick={() => open({ path: entry.path, heading: entry.heading ?? undefined })}>
          <span className="search-result-header">
            <span className="mono ellipsis" title={entry.path}>{entry.path}</span>
            {entry.heading && <><span aria-hidden="true">›</span><strong className="ellipsis">{entry.heading}</strong></>}
            <span className="mini-chip">{matchLabel(entry.match)}</span>
          </span>
          <span className="search-snippet">{highlighted(entry.snippet, query.trim())}</span>
        </button>))}
    </Panel>}
    <div id="memory-rankings" className="rankings">
      <RankPanel title={t("memory.mostUsed")} source={rankings} rows={rankings.data?.most_used.map((note) => ({
        key: note.path, label: note.title || note.path, value: note.uses, action: () => open({ path: note.path }),
      })) ?? []} />
      <RankPanel title={t("memory.mostShown")} source={rankings} rows={rankings.data?.most_shown.map((note) => ({
        key: note.path, label: note.title || note.path, value: note.impressions, action: () => open({ path: note.path }),
      })) ?? []} />
      <RankPanel title={t("memory.queries")} source={rankings} rows={rankings.data?.top_queries.map(({ text, count, avg_hits }) => ({
        key: text, label: text, value: count, detail: t("common.average", { count: number(Number(avg_hits.toFixed(1))) }),
        action: () => { change(text.slice(0, 500), true); input.current?.focus(); },
      })) ?? []} />
    </div>
    <div id="memory-recent">
      <Panel title={t("memory.recent")}>
        {recent.error && <Problem title={t("memory.recentError")} detail={recent.error} retry={() => void recent.refresh(true)} />}
        {!recent.data ? recent.loading && <LoadingRows count={5} /> : recent.data.entries.length ? recent.data.entries.map((entry, index) =>
          <NoteRow key={`${entry.path}-${index}`} title={entry.heading} path={entry.path} leading={entry.date.slice(0, 10)} trailing={<TagList tags={entry.tags} />}
            onClick={() => open({ path: entry.path, heading: entry.heading })} />) : <Empty>{t("memory.noRecords")}</Empty>}
      </Panel>
    </div>
  </div>;
}
