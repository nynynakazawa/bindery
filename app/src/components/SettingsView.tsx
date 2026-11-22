import { useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  FormControl,
  FormControlLabel,
  Paper,
  Radio,
  RadioGroup,
  Stack,
  Step,
  StepLabel,
  Stepper,
  TextField,
  Typography,
} from "@mui/material";
import ArrowBackRoundedIcon from "@mui/icons-material/ArrowBackRounded";
import CheckRoundedIcon from "@mui/icons-material/CheckRounded";
import PlayArrowRoundedIcon from "@mui/icons-material/PlayArrowRounded";
import type { CommandResult, UvInfo } from "../types";
import { t } from "../copy";
import { accentTextSx, cardSx } from "../theme";

const steps = [
  { id: "uv", label: "setup.step.uv" as const, description: "setup.uv.description" as const },
  { id: "wheel", label: "setup.step.wheel" as const, description: "setup.wheel.description" as const },
  { id: "vault", label: "setup.step.vault" as const, description: "setup.vault.description" as const },
  { id: "clients", label: "setup.step.clients" as const, description: "setup.clients.description" as const },
  { id: "sync", label: "setup.step.sync" as const, description: "setup.sync.description" as const },
  { id: "scheduler", label: "setup.step.scheduler" as const, description: "setup.scheduler.description" as const },
];

type StepId = (typeof steps)[number]["id"];

function commandFor(step: StepId, vault: string, remote: string, mode: "init" | "pair"): string {
  const quotedVault = vault ? ` --vault "${vault}"` : "";
  if (step === "uv") return "uv --version";
  if (step === "wheel") return "uv tool install <bundled-wheel>";
  if (step === "vault") return t("setup.vaultPickerCommand");
  if (step === "clients") return `bindery install claude --write${quotedVault}\nbindery install codex --write${quotedVault}\nbindery install antigravity --write${quotedVault}\nbindery prompt --global --write`;
  if (step === "sync") return mode === "init" ? `bindery sync init --remote "${remote || "<remote>"}"${quotedVault} --write` : `bindery sync pair "${remote || "<remote>"}"${quotedVault}`;
  return `bindery sync install${quotedVault} --write`;
}

export function SettingsView() {
  const [active, setActive] = useState(0);
  const [completed, setCompleted] = useState<StepId[]>([]);
  const [busy, setBusy] = useState(false);
  const [uv, setUv] = useState<UvInfo | null>(null);
  const [vault, setVault] = useState("");
  const [remote, setRemote] = useState("");
  const [mode, setMode] = useState<"init" | "pair">("init");
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const current = steps[active];
  const command = useMemo(() => commandFor(current.id, vault, remote, mode), [current.id, mode, remote, vault]);

  const markDone = (id: StepId) => {
    setCompleted((previous) => (previous.includes(id) ? previous : [...previous, id]));
    if (active < steps.length - 1) setActive(active + 1);
  };

  const run = async (action: () => Promise<CommandResult>, doneId: StepId) => {
    setBusy(true);
    setResult(null);
    try {
      const response = await action();
      setResult({ ok: response.ok, message: response.ok ? t("setup.success") : response.message || t("setup.failed") });
      if (response.ok) markDone(doneId);
    } catch (error) {
      setResult({ ok: false, message: error instanceof Error ? error.message : t("setup.failed") });
    } finally {
      setBusy(false);
    }
  };

  const runCurrent = async () => {
    if (current.id === "uv") {
      setBusy(true);
      setResult(null);
      try {
        const info = await window.bindery.checkUv();
        setUv(info);
        setResult({ ok: info.available, message: info.available ? `${t("setup.uvFound")}: ${info.path ?? "uv"}` : t("setup.uvMissing") });
        if (info.available) markDone("uv");
      } finally {
        setBusy(false);
      }
      return;
    }
    if (current.id === "wheel") return run(() => window.bindery.installWheel(), "wheel");
    if (current.id === "vault") {
      setBusy(true);
      setResult(null);
      try {
        const selected = await window.bindery.selectVault();
        if (selected) {
          setVault(selected);
          setResult({ ok: true, message: t("setup.success") });
          markDone("vault");
        }
      } finally {
        setBusy(false);
      }
      return;
    }
    if (current.id === "clients") {
      return run(async () => {
        const installed = await window.bindery.installClients();
        if (!installed.ok) return installed;
        return window.bindery.writeGlobalPrompt();
      }, "clients");
    }
    if (current.id === "sync") {
      if (!remote.trim()) {
        setResult({ ok: false, message: t("setup.remoteRequired") });
        return;
      }
      return run(() => (mode === "init" ? window.bindery.initSync(remote.trim()) : window.bindery.pairSync(remote.trim())), "sync");
    }
    return run(() => window.bindery.installScheduler(), "scheduler");
  };

  return (
    <Box sx={{ minHeight: "100vh", backgroundColor: "#000", color: "text.primary", p: { xs: 2, sm: 3 } }}>
      <Stack direction="row" spacing={1} sx={{ mb: 2, alignItems: "center" }}>
        <Button onClick={() => window.close()} startIcon={<ArrowBackRoundedIcon />} sx={{ color: "#A1A1A6", minWidth: 0 }}>{t("common.close")}</Button>
        <Box sx={{ flex: 1 }} />
        <Typography sx={{ fontWeight: 700 }}>{t("app.name")}</Typography>
      </Stack>
      <Box sx={{ maxWidth: 720, mx: "auto" }}>
        <Typography variant="h1" sx={{ ...accentTextSx }}>{t("settings.title")}</Typography>
        <Typography sx={{ color: "#A1A1A6", mt: 0.8, mb: 2.5 }}>{t("settings.subtitle")}</Typography>
        <Stepper activeStep={active} alternativeLabel sx={{ mb: 3, "& .MuiStepConnector-line": { borderColor: "rgba(255,255,255,0.12)" }, "& .MuiStepIcon-root.Mui-active": { color: "#FFB547" }, "& .MuiStepIcon-root.Mui-completed": { color: "#30D158" } }}>
          {steps.map((step) => <Step key={step.id} completed={completed.includes(step.id)}><StepLabel>{t(step.label)}</StepLabel></Step>)}
        </Stepper>

        <Paper elevation={0} sx={{ ...cardSx, p: { xs: 2, sm: 3 } }}>
          <Stack direction="row" spacing={2} sx={{ justifyContent: "space-between", alignItems: "start" }}>
            <Box>
              <Typography variant="h2">{t(current.label)}</Typography>
              <Typography sx={{ color: "#A1A1A6", mt: 0.8 }}>{t(current.description)}</Typography>
            </Box>
            {completed.includes(current.id) && <Chip icon={<CheckRoundedIcon />} label={t("setup.success")} size="small" color="success" variant="outlined" />}
          </Stack>
          <Divider sx={{ my: 2.3 }} />

          {current.id === "uv" && uv && <Alert severity={uv.available ? "success" : "error"} sx={{ mb: 2 }}>{uv.available ? `${t("setup.uvFound")}: ${uv.path}` : uv.error || t("setup.uvMissing")}</Alert>}
          {current.id === "vault" && vault && <Box sx={{ p: 1.4, mb: 2, borderRadius: 2, backgroundColor: "rgba(255,181,71,0.08)", border: "1px solid rgba(255,181,71,0.24)" }}><Typography variant="body2" sx={{ color: "#A1A1A6" }}>{t("setup.selectedVault")}</Typography><Typography noWrap sx={{ mt: 0.35, fontSize: 13 }}>{vault}</Typography></Box>}
          {current.id === "sync" && (
            <Stack spacing={2} sx={{ mb: 2 }}>
              <FormControl>
                <RadioGroup row value={mode} onChange={(event) => setMode(event.target.value as "init" | "pair")}>
                  <FormControlLabel value="init" control={<Radio size="small" />} label={t("setup.initialize")} />
                  <FormControlLabel value="pair" control={<Radio size="small" />} label={t("setup.acceptPair")} />
                </RadioGroup>
              </FormControl>
              <Typography variant="body2" sx={{ color: "#A1A1A6" }}>{mode === "init" ? t("setup.initializeHint") : t("setup.pairHint")}</Typography>
              <TextField label={t("setup.remoteLabel")} placeholder={t("setup.remotePlaceholder")} value={remote} onChange={(event) => setRemote(event.target.value)} fullWidth slotProps={{ htmlInput: { maxLength: 512 } }} />
            </Stack>
          )}

          <Typography variant="body2" sx={{ color: "#6E6E73", mb: 0.75 }}>{t("setup.command")}</Typography>
          <Box component="code" sx={{ display: "block", p: 1.35, mb: 2.3, borderRadius: 2, backgroundColor: "#070708", border: "1px solid rgba(255,255,255,0.08)", color: "#D8B477", fontSize: 11, overflowX: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{command}</Box>
          {result && <Alert severity={result.ok ? "success" : "error"} sx={{ mb: 2 }}>{result.message}</Alert>}
          <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center" }}>
            <Button disabled={active === 0 || busy} onClick={() => setActive(active - 1)}>{t("common.back")}</Button>
            <Button variant="contained" startIcon={<PlayArrowRoundedIcon />} onClick={() => void runCurrent()} disabled={busy || (current.id === "clients" && !vault)} sx={{ backgroundColor: "#FFB547", color: "#0B0B0D", "&:hover": { backgroundColor: "#ffc46f" } }}>
              {busy ? t("common.loading") : current.id === "uv" ? t("setup.checkUv") : current.id === "wheel" ? t("setup.installWheel") : current.id === "vault" ? t("setup.chooseVault") : current.id === "clients" ? t("setup.installClients") : current.id === "sync" ? (mode === "init" ? t("setup.initialize") : t("setup.acceptPair")) : t("setup.installScheduler")}
            </Button>
          </Stack>
        </Paper>
      </Box>
    </Box>
  );
}
