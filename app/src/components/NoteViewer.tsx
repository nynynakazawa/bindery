import { Button, Dialog, DialogActions, DialogContent, DialogTitle, IconButton, Tooltip } from "@mui/material";
import ArrowBackOutlined from "@mui/icons-material/ArrowBackOutlined";
import ArrowForwardOutlined from "@mui/icons-material/ArrowForwardOutlined";
import CloseOutlined from "@mui/icons-material/CloseOutlined";
import FolderOpenOutlined from "@mui/icons-material/FolderOpenOutlined";
import OpenInNewOutlined from "@mui/icons-material/OpenInNewOutlined";
import { useEffect, useMemo, useRef, useState } from "react";
import { t } from "../copy";
import { findNoteHeading, renderNoteMarkdown } from "../lib/markdown";
import type { NoteLink, NoteLocation } from "../lib/noteLinks";
import type { NoteDocument, NoteResponse } from "../types";
import { dateTime, Empty, LoadingRows, Problem, TagList } from "./DashboardParts";

export function NoteMetadata({ note }: { note: NoteDocument }) {
  const { project, tier, tags, mtime, ...custom } = note.frontmatter ?? {};
  const extras = Object.entries(custom);
  const value = (entry: unknown) => typeof entry === "object" && entry !== null ? JSON.stringify(entry) : String(entry);
  const primary = [
    [t("index.project"), project],
    [t("index.tier"), tier],
    [t("index.tags"), tags],
    [t("index.updated"), note.mtime != null ? dateTime(note.mtime) : mtime],
  ].filter(([, entry]) => entry != null && entry !== "");
  if (!primary.length && !extras.length) return null;
  const rows = (items: [string, unknown][]) => <dl className="key-values">{items.map(([key, entry]) => <div className="meta-pair" key={key}><dt>{key}</dt><dd>{value(entry)}</dd></div>)}</dl>;
  return <details className="note-meta" open>
    <summary>{t("note.meta")}</summary>
    <dl className="key-values">{primary.map(([label, entry]) => <div className="meta-pair" key={String(label)}>
      <dt>{String(label)}</dt><dd>{label === t("index.tags") && Array.isArray(tags) ? <TagList tags={tags.map(String)} /> : value(entry)}</dd>
    </div>)}</dl>
    {extras.length > 3 ? <details><summary>{t("note.metaMore", { count: extras.length })}</summary>{rows(extras)}</details> : rows(extras)}
  </details>;
}

type Load = { path: string; response: NoteResponse };

export function NoteViewer({ location, paths, open, close, back, forward, run }: {
  location: NoteLocation; paths: string[]; open: (note: NoteLocation) => void; close: () => void;
  back: (() => void) | undefined; forward: (() => void) | undefined;
  run: (action: () => Promise<void>) => Promise<void>;
}) {
  const [load, setLoad] = useState<Load>();
  const [external, setExternal] = useState<string>();
  const body = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const { path, heading } = location;
  const response = load?.path === path ? load.response : undefined;
  const note = response?.error === null ? response : undefined;

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButton.current?.focus({ preventScroll: true });
    return () => previous?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    let current = true;
    window.bindery.readNote(path).then((result) => {
      if (current) setLoad({ path, response: result });
    }, (error: unknown) => {
      if (current) setLoad({ path, response: { error: error instanceof Error ? error.message : String(error) } });
    });
    return () => { current = false; };
  }, [path]);

  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      if (event.key !== "Escape" || external || event.defaultPrevented) return;
      event.preventDefault();
      close();
    }
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [external, close]);

  const markdown = useMemo(() => {
    if (!note?.body) return null;
    const follow = (link: NoteLink) => link.kind === "external" ? setExternal(link.url) : open(link.location);
    return renderNoteMarkdown(note.body, path, paths, follow);
  }, [note, open, path, paths]);

  useEffect(() => {
    const container = body.current;
    if (!container) return;
    container.scrollTop = 0;
    if (!note || !heading) return;
    const headings = [...container.querySelectorAll<HTMLElement>("[data-heading]")];
    const match = findNoteHeading(headings, heading);
    if (!match) return;
    container.scrollTop = match.getBoundingClientRect().top - container.getBoundingClientRect().top - 16;
    match.classList.add("heading-target");
    return () => match.classList.remove("heading-target");
  }, [heading, note, path]);

  const viewerError = response?.error;
  const notFound = !response?.canReveal && viewerError && /not found|does not exist|no such file|見つかりません/iu.test(viewerError);
  return <>
    <div className="viewer-backdrop" aria-hidden="true" />
    <aside className="note-viewer" aria-label={t("note.viewer")}>
      <div className="viewer-toolbar">
        <Tooltip title={t("common.back")}><span><IconButton size="small" aria-label={t("common.back")} disabled={!back} onClick={back}><ArrowBackOutlined /></IconButton></span></Tooltip>
        <Tooltip title={t("note.forward")}><span><IconButton size="small" aria-label={t("note.forward")} disabled={!forward} onClick={forward}><ArrowForwardOutlined /></IconButton></span></Tooltip>
        <span className="mono ellipsis" title={path}>{path}</span>
        <Tooltip title={t("common.close")}><IconButton ref={closeButton} size="small" aria-label={t("common.close")} onClick={close}><CloseOutlined /></IconButton></Tooltip>
      </div>
      <div className="viewer-actions">
        {(!response || note) && <Button color="inherit" variant="outlined" size="small" disabled={!note} startIcon={<OpenInNewOutlined />}
          onClick={() => void run(() => window.bindery.openInObsidian(path))}>{t("actions.obsidian")}</Button>}
        {(!response || note || response.canReveal) && <Button color="inherit" variant="outlined" size="small" disabled={!note && !response?.canReveal}
          startIcon={<FolderOpenOutlined />} onClick={() => void run(() => window.bindery.revealInFinder(path))}>{t("actions.reveal")}</Button>}
      </div>
      <div className="viewer-body" ref={body}>
        {!response && <LoadingRows count={7} />}
        {viewerError && <Problem title={t(notFound ? "note.notFound" : "note.readError")} detail={notFound ? undefined : viewerError} />}
        {note && <><NoteMetadata note={note} />{note.body.trim() ? <article className="markdown-body">{markdown}</article> : <Empty>{t("note.empty")}</Empty>}</>}
      </div>
    </aside>
    <Dialog open={Boolean(external)} onClose={() => setExternal(undefined)} slotProps={{ paper: { className: "external-dialog" } }}>
      <DialogTitle>{t("note.externalTitle")}</DialogTitle>
      <DialogContent><p className="external-url mono" title={external}>{external}</p></DialogContent>
      <DialogActions>
        <Button color="inherit" onClick={() => setExternal(undefined)} autoFocus>{t("common.cancel")}</Button>
        <Button variant="contained" onClick={() => {
          const url = external;
          setExternal(undefined);
          if (url) void run(() => window.bindery.openExternal(url));
        }}>{t("note.externalAction")}</Button>
      </DialogActions>
    </Dialog>
  </>;
}
