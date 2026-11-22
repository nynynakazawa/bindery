import { useEffect, useState } from "react";
import { Button, ButtonBase, useMediaQuery } from "@mui/material";
import ChevronRightOutlined from "@mui/icons-material/ChevronRightOutlined";
import { t } from "../copy";
import type { BinderySnapshot, RankingData, RecentEntry } from "../types";
import type { ListState } from "../lib/dashboardData";
import type { NoteLocation } from "../lib/noteLinks";
import { clientLabels } from "../lib/presentation";
import { Empty, LoadingRows, NoteRow, Panel, Problem, SyncIndicator, number, relativeTime, unavailable } from "./DashboardParts";

export function OverviewView({ snapshot, rankings, recent, syncing, go, open, refresh, error }: {
  snapshot?: BinderySnapshot;
  rankings: ListState<RankingData>;
  recent: ListState<{ entries: RecentEntry[] }>;
  syncing: boolean;
  go: (page: "connections" | "index" | "sync" | "settings" | "memory", section?: string) => void;
  open: (note: NoteLocation) => void;
  refresh: () => void;
  error?: string;
}) {
  const [active, setActive] = useState(0);
  const reduceMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const clients = Object.entries(clientLabels).map(([id, label]) => ({
    label,
    count: snapshot?.clients.connections.filter((connection) => connection.client === id).length ?? 0,
  }));
  const connected = clients.filter(({ count }) => count > 0);
  useEffect(() => {
    if (reduceMotion || connected.length < 2) return;
    const timer = setInterval(() => setActive((index) => index + 1), 6000);
    return () => clearInterval(timer);
  }, [connected.length, reduceMotion]);

  const status = snapshot?.status;
  const sync = snapshot?.sync;
  const attention = (sync?.held_secrets ?? 0) + (sync?.conflicts ?? 0) + Number(Boolean(sync?.blocked_reason));
  const all = (section: string) => <Button color="inherit" size="small" endIcon={<ChevronRightOutlined />} onClick={() => go("memory", section)}>{t("common.all")}</Button>;

  return <div className="section-stack">
    {error && <Problem title={t("status.refreshFailed")} detail={error} retry={refresh} />}
    {status && !status.vault && <div className="notice"><span>{t("overview.noVault")}</span><Button color="inherit" onClick={() => go("settings")}>{t("actions.settings")}</Button></div>}
    <div className="overview-top">
      <Panel className="hero-panel" title={t("hero.connectedTo")}>
        {!snapshot ? <LoadingRows /> : snapshot.clients.error ? <Problem title={t("overview.clientsError")} retry={refresh} /> :
          <ButtonBase className="summary-action hero-action" onClick={() => go("connections")}>
            {connected.length === 0 ? <span className="hero-waiting">{t("hero.waiting")}</span> : reduceMotion ?
              <span className="hero-static">{connected.map(({ label }) => <span key={label} className="glow-text">{label}</span>)}</span> :
              <span className="glow-text hero-slide" key={connected[active % connected.length].label}>{connected[active % connected.length].label}</span>}
            <span className="client-counts">{clients.map(({ label, count }) => <span key={label}>{label} <b>{count}</b></span>)}</span>
          </ButtonBase>}
      </Panel>
      <Panel title={t("overview.memoryIndex")} className="summary-panel">
        {!status ? <LoadingRows /> : status.error ? <Problem title={t("overview.indexError")} retry={refresh} /> :
          <ButtonBase className="summary-action" onClick={() => go("index")}>
            <span className="hero-number">{number(status.notes)} <small>{t("vault.notes")}</small></span>
            <span className="overview-metrics">
              <span><b>{number(status.passages)}</b> {t("index.chunks")}</span>
              <span><b>{number(status.embedded)}</b> {t("vault.embedded")}</span>
            </span>
          </ButtonBase>}
      </Panel>
      <Panel title={t("overview.syncStatus")} className="summary-panel">
        {!sync ? <LoadingRows /> : sync.error ? <Problem title={t("sync.loadError")} retry={refresh} /> :
          <ButtonBase className="summary-action" onClick={() => go("sync", attention ? "sync-attention" : undefined)}>
            <SyncIndicator sync={sync} running={syncing} />
            <span className="overview-sync-info">
              <span>{t("sync.lastSuccess")} <b>{relativeTime(sync.last_success) || unavailable}</b></span>
              <span className="ellipsis" title={sync.peers.join(", ")}>{t("sync.peers")} <b>{sync.peers.join(", ") || unavailable}</b></span>
            </span>
            <span className={`attention-count ${attention ? "has-attention" : ""} ${sync.blocked_reason ? "is-blocked" : ""}`}>
              {t("sync.noticeCount", { count: attention })}
              {Boolean(sync.held_secrets) && <span>{t("sync.heldSecrets")} {number(sync.held_secrets)}</span>}
              {Boolean(sync.conflicts) && <span>{t("sync.conflicts")} {number(sync.conflicts)}</span>}
              {sync.blocked_reason && <span>{t("sync.blockedReason")}</span>}
            </span>
          </ButtonBase>}
      </Panel>
    </div>
    <div className="overview-lists">
      <Panel title={t("memory.mostUsed")} action={all("memory-rankings")}>
        {rankings.error && <Problem title={t("memory.rankingError")} retry={() => void rankings.refresh(true)} />}
        {!rankings.data ? rankings.loading && <LoadingRows count={5} /> : rankings.data.most_used.length ? rankings.data.most_used.slice(0, 5).map((note, index) =>
          <NoteRow key={note.path} title={note.title || note.path} path={note.path} leading={index + 1} trailing={t("common.times", { count: number(note.uses) })} onClick={() => open({ path: note.path })} />) :
          <Empty>{t("overview.noNotes")}</Empty>}
      </Panel>
      <Panel title={t("memory.recent")} action={all("memory-recent")}>
        {recent.error && <Problem title={t("memory.recentError")} retry={() => void recent.refresh(true)} />}
        {!recent.data ? recent.loading && <LoadingRows count={5} /> : recent.data.entries.length ? recent.data.entries.slice(0, 5).map((entry, index) =>
          <NoteRow key={`${entry.path}-${index}`} title={entry.heading} path={entry.path} leading={entry.date.slice(5, 10).replace("-", "/")} onClick={() => open({ path: entry.path, heading: entry.heading })} />) :
          <Empty>{t("overview.noRecent")}</Empty>}
      </Panel>
    </div>
  </div>;
}
