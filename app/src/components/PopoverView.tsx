import { useEffect, useMemo, useState } from "react";
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  IconButton,
  Paper,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import SyncIcon from "@mui/icons-material/Sync";
import SettingsOutlinedIcon from "@mui/icons-material/SettingsOutlined";
import FolderOpenOutlinedIcon from "@mui/icons-material/FolderOpenOutlined";
import ArticleOutlinedIcon from "@mui/icons-material/ArticleOutlined";
import TuneOutlinedIcon from "@mui/icons-material/TuneOutlined";
import type { BinderySnapshot, ClientInfo, SyncInfo } from "../types";
import { t } from "../copy";
import { formatClientState, formatConnectedCliNames, formatSyncState } from "../lib/presentation";
import { accentTextSx, cardSx } from "../theme";
import { StatusDot } from "./StatusDot";

const clientOrder = [
  { id: "claude", fallback: "Claude Code" },
  { id: "codex", fallback: "Codex" },
  { id: "antigravity", fallback: "Antigravity" },
];

const stateColor: Record<string, string> = {
  connected: "#30D158",
  registered: "#FFB547",
  detected: "#FFD60A",
  missing: "#6E6E73",
  ok: "#30D158",
  error: "#FF453A",
  blocked: "#FF453A",
  never: "#6E6E73",
};

function number(value: number | null): string {
  return value === null ? t("common.notAvailable") : value.toLocaleString();
}

function time(value: string | null): string {
  if (!value) return t("sync.never");
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function clientById(clients: ClientInfo[], id: string): ClientInfo {
  return (
    clients.find((client) => client.id === id) ?? {
      id,
      label: clientOrder.find((entry) => entry.id === id)?.fallback ?? id,
      detected: false,
      registered: false,
      config_path: "",
      vault: null,
    }
  );
}

function syncLabel(sync: SyncInfo): string {
  const state = formatSyncState(sync);
  return t(`sync.state.${state}` as "sync.state.ok");
}

export function PopoverView({ snapshot, onRefresh }: { snapshot: BinderySnapshot | null; onRefresh: () => Promise<void> }) {
  const [activeIndex, setActiveIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const connected = useMemo(() => (snapshot ? formatConnectedCliNames(snapshot.clients) : []), [snapshot]);
  const connectionCount = snapshot?.clients.connections.length ?? 0;
  const activeName = connected.length ? connected[activeIndex % connected.length] : t("hero.waiting");
  const syncState = snapshot ? formatSyncState(snapshot.sync) : "never";

  useEffect(() => {
    if (connected.length < 2) return;
    const timer = window.setInterval(() => setActiveIndex((index) => index + 1), 6000);
    return () => window.clearInterval(timer);
  }, [connected.length]);

  const refresh = async () => {
    setBusy(true);
    try {
      await onRefresh();
    } finally {
      setBusy(false);
    }
  };

  if (!snapshot) {
    return (
      <Box sx={{ minHeight: "100vh", display: "grid", placeItems: "center", backgroundColor: "#000" }}>
        <CircularProgress size={22} sx={{ color: "#FFB547" }} />
      </Box>
    );
  }

  return (
    <Box sx={{ minHeight: "100vh", px: 1.5, py: 1.5, backgroundColor: "#000", color: "text.primary" }}>
      <Stack direction="row" sx={{ px: 0.5, mb: 1.2, alignItems: "center", justifyContent: "space-between" }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <Box sx={{ width: 8, height: 8, borderRadius: "50%", background: "linear-gradient(135deg, #FFB547, #B77CFF)" }} />
          <Typography sx={{ fontWeight: 700, fontSize: 14, letterSpacing: "-0.02em" }}>{t("app.name")}</Typography>
        </Stack>
        <Stack direction="row" spacing={0.2}>
          <Tooltip title={t("actions.settings")}>
            <IconButton size="small" onClick={() => void window.bindery.showSettings()} aria-label={t("actions.settings")}>
              <SettingsOutlinedIcon sx={{ fontSize: 17, color: "#A1A1A6" }} />
            </IconButton>
          </Tooltip>
          <Tooltip title={t("actions.syncNow")}>
            <IconButton size="small" onClick={() => void refresh()} disabled={busy} aria-label={t("actions.syncNow")}>
              {busy ? <CircularProgress size={16} sx={{ color: "#FFB547" }} /> : <SyncIcon sx={{ fontSize: 17, color: "#A1A1A6" }} />}
            </IconButton>
          </Tooltip>
        </Stack>
      </Stack>

      <Paper elevation={0} sx={{ ...cardSx, minHeight: 126, overflow: "hidden", position: "relative", p: 2, mb: 1.25 }}>
        <Box
          sx={{
            position: "absolute",
            inset: 0,
            opacity: 0.45,
            background: "radial-gradient(ellipse at 20% 0%, rgba(255,181,71,0.20), transparent 55%), radial-gradient(ellipse at 90% 100%, rgba(183,124,255,0.15), transparent 55%)",
            pointerEvents: "none",
          }}
        />
        <Typography variant="body2" sx={{ color: "#A1A1A6", position: "relative" }}>{t("hero.connectedTo")}</Typography>
        <Typography
          key={activeName}
          component="div"
          variant="h1"
          sx={{ ...accentTextSx, mt: 1.6, position: "relative", animation: "bindery-slide 720ms cubic-bezier(0.68,-0.55,0.265,1.55) both", "@keyframes bindery-slide": { from: { opacity: 0, transform: "translateX(34px)" }, to: { opacity: 1, transform: "translateX(0)" } }, "@media (prefers-reduced-motion: reduce)": { animation: "none" } }}
        >
          {activeName}
        </Typography>
        <Typography variant="body2" sx={{ color: "#6E6E73", mt: 0.6, position: "relative" }}>
          {connectionCount} {t("hero.connectedCount")}
        </Typography>
      </Paper>

      <Paper elevation={0} sx={{ ...cardSx, p: 1.25, mb: 1.25 }}>
        <Typography variant="body2" sx={{ color: "#6E6E73", px: 0.75, pb: 0.55 }}>{t("clients.title")}</Typography>
        <Stack divider={<Divider flexItem sx={{ borderColor: "rgba(255,255,255,0.055)" }} />}>
          {clientOrder.map(({ id }) => {
            const client = clientById(snapshot.clients.clients, id);
            const state = formatClientState(client, snapshot.clients.connections, id);
            const stateLabel = state === "connected" ? t("clients.connection") : state === "registered" ? t("clients.registered") : state === "detected" ? t("clients.detected") : t("clients.notDetected");
            return (
              <Stack key={id} direction="row" sx={{ minHeight: 34, px: 0.75, alignItems: "center", justifyContent: "space-between" }}>
                <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
                  <StatusDot color={stateColor[state]} pulse={state === "connected"} />
                  <Typography sx={{ fontSize: 12 }}>{client.label}</Typography>
                </Stack>
                <Chip label={stateLabel} size="small" sx={{ height: 21, fontSize: 10, color: stateColor[state], backgroundColor: `${stateColor[state]}18`, border: `1px solid ${stateColor[state]}35` }} />
              </Stack>
            );
          })}
        </Stack>
      </Paper>

      <Paper elevation={0} sx={{ ...cardSx, p: 1.5, mb: 1.25 }}>
        <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "start" }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="body2" sx={{ color: "#6E6E73" }}>{t("vault.title")}</Typography>
            <Typography noWrap sx={{ fontSize: 12, mt: 0.4, maxWidth: 245 }}>{snapshot.status.vault || t("vault.notSelected")}</Typography>
          </Box>
          <Tooltip title={t("actions.openVault")}>
            <IconButton size="small" onClick={() => void window.bindery.openVault()} aria-label={t("actions.openVault")}>
              <FolderOpenOutlinedIcon sx={{ fontSize: 17, color: "#A1A1A6" }} />
            </IconButton>
          </Tooltip>
        </Stack>
        <Stack direction="row" spacing={2.8} sx={{ mt: 1.5 }}>
          {[ [t("vault.notes"), snapshot.status.notes], [t("vault.passages"), snapshot.status.passages], [t("vault.embedded"), snapshot.status.embedded] ].map(([label, value]) => (
            <Box key={String(label)}>
              <Typography sx={{ fontSize: 25, lineHeight: 1, fontWeight: 650, letterSpacing: "-0.04em" }}>{number(value as number | null)}</Typography>
              <Typography variant="body2" sx={{ color: "#6E6E73", mt: 0.45 }}>{label}</Typography>
            </Box>
          ))}
        </Stack>
      </Paper>

      <Paper elevation={0} sx={{ ...cardSx, p: 1.5, mb: 1.25 }}>
        <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center" }}>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
            <StatusDot color={stateColor[syncState]} pulse={syncState === "ok"} />
            <Typography sx={{ fontSize: 13, fontWeight: 650 }}>{t("sync.title")}</Typography>
          </Stack>
          <Chip label={syncLabel(snapshot.sync)} size="small" sx={{ height: 21, fontSize: 10, color: stateColor[syncState], backgroundColor: `${stateColor[syncState]}18` }} />
        </Stack>
        <Stack direction="row" sx={{ mt: 1.2, justifyContent: "space-between" }}>
          <Box><Typography variant="body2" sx={{ color: "#6E6E73" }}>{t("sync.lastSuccess")}</Typography><Typography sx={{ fontSize: 11, mt: 0.25 }}>{time(snapshot.sync.last_success)}</Typography></Box>
          <Box sx={{ textAlign: "right" }}><Typography variant="body2" sx={{ color: "#6E6E73" }}>{t("sync.remote")}</Typography><Typography noWrap sx={{ fontSize: 11, maxWidth: 140, mt: 0.25 }}>{snapshot.sync.remote || t("common.notAvailable")}</Typography></Box>
        </Stack>
        {(snapshot.sync.conflicts > 0 || snapshot.sync.held_secrets > 0 || snapshot.sync.blocked_reason) && (
          <Stack spacing={0.35} sx={{ mt: 1.2, color: "#FFD60A" }}>
            {snapshot.sync.conflicts > 0 && <Typography variant="body2">{t("sync.conflicts")}: {snapshot.sync.conflicts}</Typography>}
            {snapshot.sync.held_secrets > 0 && <Typography variant="body2">{t("sync.heldSecrets")}: {snapshot.sync.held_secrets}</Typography>}
            {snapshot.sync.blocked_reason && <Typography variant="body2" noWrap>{t("sync.blockedReason")}: {snapshot.sync.blocked_reason}</Typography>}
          </Stack>
        )}
      </Paper>

      <Stack direction="row" spacing={0.75} sx={{ pb: 0.5 }}>
        <Button fullWidth variant="contained" startIcon={<SyncIcon sx={{ fontSize: 16 }} />} onClick={() => void refresh()} disabled={busy} sx={{ backgroundColor: "#FFB547", color: "#0B0B0D", "&:hover": { backgroundColor: "#ffc46f" } }}>{t("actions.syncNow")}</Button>
        <Tooltip title={t("actions.openLog")}>
          <IconButton onClick={() => void window.bindery.openLog()} sx={{ border: "1px solid rgba(255,255,255,0.10)", borderRadius: 3 }} aria-label={t("actions.openLog")}><ArticleOutlinedIcon sx={{ fontSize: 18 }} /></IconButton>
        </Tooltip>
        <Tooltip title={t("actions.settings")}>
          <IconButton onClick={() => void window.bindery.showSettings()} sx={{ border: "1px solid rgba(255,255,255,0.10)", borderRadius: 3 }} aria-label={t("actions.settings")}><TuneOutlinedIcon sx={{ fontSize: 18 }} /></IconButton>
        </Tooltip>
      </Stack>
    </Box>
  );
}
