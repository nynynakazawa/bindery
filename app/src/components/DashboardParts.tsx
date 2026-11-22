import { Button, ButtonBase, Skeleton } from "@mui/material";
import type { ReactNode } from "react";
import { language, t } from "../copy";
import { formatSyncState } from "../lib/presentation";
import type { SyncInfo } from "../types";
import { StatusDot } from "./StatusDot";

export const unavailable = t("common.notAvailable");
export const number = (value?: number | null) => value == null ? unavailable : value.toLocaleString(language);

function dateFor(value: string | number | null | undefined) {
  if (value == null) return null;
  const date = new Date(typeof value === "number" && Math.abs(value) < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function dateTime(value: string | number | null | undefined, format: "full" | "short" | "time" = "full") {
  const date = dateFor(value);
  if (!date) return unavailable;
  if (format === "time") return date.toLocaleTimeString(language, { hour: "2-digit", minute: "2-digit", hour12: false });
  const currentYear = date.getFullYear() === new Date().getFullYear();
  const options: Intl.DateTimeFormatOptions = { month: "2-digit", day: "2-digit" };
  if (format === "full" || !currentYear) options.year = "numeric";
  if (format === "full" || currentYear) Object.assign(options, { hour: "2-digit", minute: "2-digit", hour12: false });
  return date.toLocaleString(language, options);
}

export function relativeTime(value?: string | null) {
  const date = dateFor(value);
  if (!date) return "";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(language, { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  if (Math.abs(seconds) < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

export function syncPresentation(sync: SyncInfo | undefined, running: boolean) {
  const state = sync ? formatSyncState(sync) : "never";
  if (running) return { label: t("sync.running"), color: "var(--accent)" };
  if (state === "blocked" || state === "error") return { label: t("sync.stopped"), color: "var(--error)", attention: true };
  if (sync?.conflicts || sync?.held_secrets) return { label: t("sync.attention"), color: "var(--warning)", attention: true };
  if (state === "ok") return { label: t("sync.latest"), color: "var(--success)" };
  return { label: t("sync.pending"), color: "var(--muted)" };
}

export function SyncIndicator({ sync, running }: { sync?: SyncInfo; running: boolean }) {
  const { label, color } = syncPresentation(sync, running);
  return <span className="sync-indicator"><StatusDot color={color} pulse={running} />{label}</span>;
}

export function Panel({ title, action, children, className = "" }: { title?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>
    {title && <div className="panel-heading"><h2>{title}</h2>{action}</div>}
    {children}
  </section>;
}

export function LoadingRows({ count = 3 }: { count?: number }) {
  return <div className="loading-rows" aria-label={t("common.loading")} role="status">
    {Array.from({ length: count }, (_, index) => <Skeleton key={index} variant="rounded" height={24} width={`${100 - (index % 3) * 12}%`} />)}
  </div>;
}

export function Problem({ title, detail, retry, action }: { title: string; detail?: string | null; retry?: () => void; action?: ReactNode }) {
  return <div className="problem" role="alert">
    <div><strong>{title}</strong>{detail && <div className="problem-detail">{detail}</div>}</div>
    {(retry || action) && <div className="problem-actions">{retry && <Button color="inherit" onClick={retry}>{t("common.retry")}</Button>}{action}</div>}
  </div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty-state">{children}</p>;
}

export function NoteRow({ title, path, leading, trailing, children, onClick }: {
  title: string; path?: string; leading?: ReactNode; trailing?: ReactNode; children?: ReactNode; onClick: () => void;
}) {
  return <ButtonBase className="note-row" onClick={onClick}>
    {leading && <span className="note-row-leading mono">{leading}</span>}
    <span className="note-row-main">
      <span className="note-row-title ellipsis">{title}</span>
      {path && <span className="note-row-path ellipsis mono" title={path}>{path}</span>}
      {children}
    </span>
    {trailing && <span className="note-row-trailing">{trailing}</span>}
  </ButtonBase>;
}

export function TagList({ tags, limit = 3 }: { tags: string[]; limit?: number }) {
  return <span className="tags" title={tags.join(", ")}>
    {tags.slice(0, limit).map((tag, index) => <span className="mini-chip" key={`${tag}-${index}`}>{tag}</span>)}
    {tags.length > limit && <span className="mini-chip">+{tags.length - limit}</span>}
  </span>;
}
