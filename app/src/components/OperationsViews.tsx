import { Button, ButtonBase, Chip, IconButton, LinearProgress, Tooltip } from "@mui/material";
import ContentCopyOutlined from "@mui/icons-material/ContentCopyOutlined";
import FolderOpenOutlined from "@mui/icons-material/FolderOpenOutlined";
import CheckOutlined from "@mui/icons-material/CheckOutlined";
import { t } from "../copy";
import type { BinderySnapshot, CommandResult } from "../types";
import { clientLabels, formatSyncState, syncAttentionCount } from "../lib/presentation";
import type { NoteLocation } from "../lib/noteLinks";
import { dateTime, LoadingRows, Panel, Problem, relativeTime, SyncIndicator, unavailable } from "./DashboardParts";
import { StatusDot } from "./StatusDot";

type RunAction = (action: () => Promise<CommandResult | void>) => Promise<void>;

const compactPath = (path: string) => path.replace(/^\/(Users|home)\/[^/]+(?=\/|$)/u, "~");

export function ConnectionsView({ snapshot, register, registerError, refresh, run }: {
  snapshot?: BinderySnapshot; register: () => void; registerError?: string; refresh: () => void; run: RunAction;
}) {
  const vault = snapshot?.status.vault;
  const clients = snapshot?.clients;
  const registeredVaults = new Set(clients?.clients.filter((client) => client.registered && client.vault).map((client) => client.vault));
  const ids: [string, string][] = Object.entries(clientLabels);
  clients?.clients.forEach(({ id, label }) => {
    if (!Object.hasOwn(clientLabels, id)) ids.push([id, label]);
  });

  return <div className="section-stack">
    <Panel className="vault-bar">
      <div className="ellipsis"><span className="meta">{t("clients.defaultVault")}</span><div className="mono ellipsis" title={vault}>{vault || unavailable}</div></div>
      <Tooltip title={t("common.copy")}>
        <span><IconButton size="small" aria-label={t("common.copy")} disabled={!vault} onClick={() => void run(() => navigator.clipboard.writeText(vault!))}><ContentCopyOutlined /></IconButton></span>
      </Tooltip>
      <Button color="inherit" variant="outlined" disabled={!snapshot?.status.vault_exists} startIcon={<FolderOpenOutlined />} onClick={() => void run(window.bindery.openVault)}>{t("actions.reveal")}</Button>
    </Panel>
    {registeredVaults.size > 1 && <div className="notice warning" role="status">{t("clients.mismatch")}</div>}
    {registerError && <Problem title={t("clients.registerError")} detail={registerError} retry={register} />}
    {clients?.error && <Problem title={t("overview.clientsError")} detail={clients.error} retry={refresh} />}
    {ids.map(([id, fallback]) => {
      const client = clients?.clients.find((entry) => entry.id === id);
      const processes = clients?.connections.filter((entry) => entry.client === id) ?? [];
      const mismatch = Boolean(client?.registered && client.vault && vault && client.vault !== vault);
      return <Panel key={id} className={client?.detected === false ? "client-missing" : ""}>
        <div className="client-heading">
          <h2>{client?.label || fallback}</h2>
          {clients && !clients.error && <>
            <Chip size="small" variant="outlined" label={t(client?.detected ? "clients.detected" : "clients.notDetected")} />
            <Chip size="small" variant="outlined" label={t(client?.registered ? "clients.registered" : "clients.notRegistered")} />
            <span className="client-connection-count">{processes.length > 0 && <StatusDot color="var(--success)" />}{t("clients.count", { count: processes.length })}</span>
          </>}
        </div>
        {!clients ? <LoadingRows count={3} /> : !clients.error && <>
          <dl className="key-values">
            <dt>{t("clients.registeredVault")}</dt>
            <dd className={`mono ${mismatch ? "warning-text" : ""}`} title={client?.vault ?? undefined}>{client?.vault || unavailable}</dd>
            <dt>{t("clients.config")}</dt>
            <dd className="mono" title={client?.config_path}>{client?.config_path ? compactPath(client.config_path) : unavailable}</dd>
          </dl>
          <div className="process-list">
            <div className="meta">{t("clients.processes")}</div>
            {processes.length === 0 ? <div className="meta">{t("clients.none")}</div> : processes.map(({ pid, started_at }) =>
              <div className="process-row" key={pid}>
                <StatusDot color="var(--success)" />
                <span className="mono">PID {pid}</span>
                <span>{started_at ? t("clients.started", { time: dateTime(started_at, "time"), relative: relativeTime(started_at) }) : unavailable}</span>
              </div>)}
          </div>
        </>}
      </Panel>;
    })}
  </div>;
}

export function SyncView({ snapshot, syncing, error, retry, refresh, goSettings, open, run }: {
  snapshot?: BinderySnapshot; syncing: boolean; error?: string; retry: () => void; refresh: () => void;
  goSettings: () => void; open: (note: NoteLocation) => void; run: RunAction;
}) {
  const sync = snapshot?.sync;
  if (!sync) return <div className="section-stack"><Panel>{error ? <Problem title={t("sync.loadError")} detail={error} retry={refresh} /> : <LoadingRows />}</Panel><Panel><LoadingRows count={5} /></Panel></div>;

  const paths = [
    ...(sync.held_paths ?? []).map((path) => ({ path, label: t("sync.heldSecrets") })),
    ...(sync.conflict_paths ?? []).map((path) => ({ path, label: t("sync.conflicts") })),
  ];
  const heldPaths = new Set(sync.held_paths);
  const syncedSecrets = sync.secret_paths?.filter((path) => !heldPaths.has(path)) ?? [];
  const state = formatSyncState(sync);
  const stopped = state === "blocked" || state === "error";
  const scheduler = t(`sync.scheduler.${sync.scheduler}`);
  const interval = sync.interval_seconds != null ? ` · ${t("sync.interval", { count: sync.interval_seconds })}` : "";
  const lastError = error || sync.error || sync.last_error;

  return <div className="section-stack">
    <Panel className="sync-summary">
      <div className="sync-summary-line"><SyncIndicator sync={sync} running={syncing} /><span>{t("sync.lastSuccess")} <b className="mono">{dateTime(sync.last_success)}</b></span></div>
      {sync.blocked_reason && <div className="blocked-reason"><strong>{t("sync.blockedReason")}</strong><span>{sync.blocked_reason}</span></div>}
      {syncing && <LinearProgress sx={{ height: 2 }} />}
    </Panel>
    {lastError && <Problem title={t("sync.loadError")} detail={lastError} retry={sync.configured ? retry : refresh}
      action={<Button color="inherit" onClick={() => void run(window.bindery.openLog)}>{t("actions.openLog")}</Button>} />}
    {!sync.configured && !sync.error && <div className="notice"><span>{t("sync.unconfigured")}</span><Button color="inherit" onClick={goSettings}>{t("sync.configure")}</Button></div>}
    <div id="sync-attention">
      {syncAttentionCount(sync) ? <Panel title={t("sync.attention")}>
        {stopped && <div className="attention-row"><span className="mini-chip error-outline">{t("sync.stopped")}</span><span>{sync.blocked_reason || sync.last_error || sync.error}</span></div>}
        {paths.map(({ path, label }) => <div className="attention-row" key={`${label}:${path}`}>
          <ButtonBase className="attention-file" onClick={() => open({ path })}>
            <span className="mini-chip warning-outline">{label}</span><span className="mono ellipsis" title={path}>{path}</span>
          </ButtonBase>
          <Button color="inherit" variant="outlined" onClick={() => void run(() => window.bindery.revealInFinder(path))}>{t("actions.reveal")}</Button>
        </div>)}
        {!sync.conflict_paths?.length && Boolean(sync.conflicts) && <div className="attention-row">{t("sync.conflicts")} · {t("common.count", { count: sync.conflicts })}</div>}
        {!sync.held_paths?.length && Boolean(sync.held_secrets) && <div className="attention-row">{t("sync.heldSecrets")} · {t("common.count", { count: sync.held_secrets })}</div>}
      </Panel> : <Panel className="nothing-to-check"><CheckOutlined />{t("sync.noAttention")}</Panel>}
    </div>
    {syncedSecrets.length > 0 && <Panel title={t("sync.syncedSecrets")} className="sync-secret-notes">
      {syncedSecrets.map((path) => <ButtonBase className="sync-secret-note" key={path} onClick={() => open({ path })}>
        <span className="mono ellipsis" title={path}>{path}</span>
      </ButtonBase>)}
    </Panel>}
    <Panel title={t("sync.details")}>
      <div className="sync-details">
        <dl className="key-values">
          <dt>{t("sync.git")}</dt><dd>{t(`sync.value.${sync.git}`)}</dd>
          <dt>{t("sync.indexState")}</dt><dd>{t(`sync.value.${sync.index}`)}</dd>
          <dt>{t("sync.lastSuccess")}</dt><dd className="mono">{dateTime(sync.last_success)}</dd>
          <dt>{t("sync.lastAttempt")}</dt><dd className="mono">{dateTime(sync.last_attempt)}</dd>
        </dl>
        <dl className="key-values">
          <dt>{t("sync.machine")}</dt><dd className="mono">{sync.machine || unavailable}</dd>
          <dt>{t("sync.peers")}</dt><dd className="mono">{sync.peers.join(", ") || unavailable}</dd>
          <dt>{t("sync.remote")}</dt><dd className="mono">{sync.remote || unavailable}</dd>
          <dt>{t("sync.keyStore")}</dt><dd>{sync.key_store === "keychain" ? t("sync.keychain") : sync.key_store === "file" ? t("sync.keyFile") : unavailable}</dd>
          <dt>{t("sync.scheduler")}</dt><dd>{scheduler}{interval}</dd>
        </dl>
      </div>
    </Panel>
  </div>;
}
