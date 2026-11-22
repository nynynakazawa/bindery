import { useEffect, useState } from "react";
import {
  Alert, Button, CircularProgress, FormControlLabel, Radio, RadioGroup, Step, StepButton, Stepper, Switch, TextField,
} from "@mui/material";
import CheckOutlined from "@mui/icons-material/CheckOutlined";
import PlayArrowOutlined from "@mui/icons-material/PlayArrowOutlined";
import type { BinderySnapshot, CommandResult, UvInfo } from "../types";
import { t } from "../copy";
import { clientLabels } from "../lib/presentation";
import { Panel } from "./DashboardParts";

const steps = ["uv", "wheel", "vault", "clients", "sync", "scheduler"] as const;
type StepId = (typeof steps)[number];

function commandFor(step: StepId, vault: string, remote: string, mode: "init" | "pair"): string {
  const quotedVault = vault ? ` --vault ${JSON.stringify(vault)}` : "";
  switch (step) {
    case "uv": return "uv --version";
    case "wheel": return "uv tool install <bundled-wheel>";
    case "vault": return t("setup.vaultPickerCommand");
    case "clients": return [...Object.keys(clientLabels).map((client) => `bindery install ${client} --write${quotedVault}`), "bindery prompt --global --write"].join("\n");
    case "sync": return mode === "init" ? `bindery sync init --remote ${JSON.stringify(remote || "<remote>")}${quotedVault} --write` : `bindery sync pair ${JSON.stringify(remote || "<remote>")}${quotedVault}`;
    case "scheduler": return `bindery sync install${quotedVault} --write`;
  }
}

export function SettingsView({ snapshot, onChanged, runAction }: {
  snapshot?: BinderySnapshot; onChanged: () => void;
  runAction: (action: () => Promise<CommandResult>) => Promise<void>;
}) {
  const api = window.bindery;
  const [activeStep, setActiveStep] = useState<StepId>();
  const [completedHere, setCompletedHere] = useState<StepId[]>([]);
  const [busy, setBusy] = useState(false);
  const [uv, setUv] = useState<UvInfo>();
  const [uvChecking, setUvChecking] = useState(true);
  const [vaultChoice, setVaultChoice] = useState("");
  const [remote, setRemote] = useState("");
  const [mode, setMode] = useState<"init" | "pair">("init");
  const [result, setResult] = useState<{ ok: boolean; message: string }>();
  const [login, setLogin] = useState<boolean>();
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<string>();

  useEffect(() => {
    let current = true;
    api.checkUv().then((info) => { if (current) setUv(info); }, () => undefined).finally(() => { if (current) setUvChecking(false); });
    api.getLoginItem().then((enabled) => { if (current) setLogin(enabled); }, () => { if (current) setLoginError(t("settings.loginReadFailed")); });
    return () => { current = false; };
  }, [api]);

  const vault = vaultChoice || snapshot?.status.vault || "";
  const registered = snapshot?.clients.clients;
  const matchesExistingVault = !vaultChoice || vaultChoice === snapshot?.status.vault;
  const inferred: Record<StepId, boolean> = {
    uv: Boolean(uv?.available),
    wheel: Boolean(snapshot?.status.version && snapshot.status.version !== "unknown"),
    vault: Boolean(vaultChoice || snapshot?.status.vault_exists),
    clients: matchesExistingVault && Object.keys(clientLabels).every((id) => registered?.some((client) => client.id === id && client.registered && client.vault === vault)),
    sync: matchesExistingVault && Boolean(snapshot?.sync.configured),
    scheduler: matchesExistingVault && snapshot?.sync.scheduler === "installed",
  };
  const done = (id: StepId) => inferred[id] || completedHere.includes(id);
  const firstIncomplete = steps.find((id) => !done(id));
  const current = activeStep ?? firstIncomplete;
  const active = current ? steps.indexOf(current) : -1;

  function select(id: StepId) {
    setResult(undefined);
    setActiveStep(id);
  }

  async function runCurrent() {
    if (!current || busy) return;
    setBusy(true);
    setResult(undefined);
    try {
      let response: CommandResult;
      switch (current) {
        case "uv": {
          const info = await api.checkUv();
          setUv(info);
          response = { ok: info.available, message: info.error || t("setup.uvMissing") };
          break;
        }
        case "wheel":
          response = await api.installWheel();
          break;
        case "vault": {
          const selected = await api.selectVault();
          if (!selected) return;
          setVaultChoice(selected);
          if (selected !== vault) setCompletedHere((prior) => prior.filter((id) => id === "uv" || id === "wheel"));
          response = { ok: true, message: t("setup.success") };
          break;
        }
        case "clients": {
          response = await api.installClients();
          if (response.ok) response = await api.writeGlobalPrompt();
          break;
        }
        case "sync":
          response = remote.trim()
            ? await (mode === "init" ? api.initSync(remote.trim()) : api.pairSync(remote.trim()))
            : { ok: false, message: t("setup.remoteRequired") };
          break;
        case "scheduler":
          response = await api.installScheduler();
          break;
      }
      setResult({ ok: response.ok, message: response.ok ? t("setup.success") : response.message || t("setup.failed") });
      if (response.ok) {
        setCompletedHere((prior) => prior.includes(current) ? prior : [...prior, current]);
        setActiveStep(steps[active + 1]);
        if (current !== "uv" && current !== "vault") onChanged();
      }
    } catch (error) {
      setResult({ ok: false, message: error instanceof Error ? error.message : t("setup.failed") });
    } finally {
      setBusy(false);
    }
  }

  async function updateLogin(enabled: boolean) {
    setLoginBusy(true);
    setLoginError(undefined);
    try {
      setLogin(await api.setLoginItem(enabled));
    } catch {
      setLoginError(t("settings.loginFailed"));
    } finally {
      setLoginBusy(false);
    }
  }

  const actionLabel = {
    uv: t("setup.checkUv"), wheel: t("setup.installWheel"), vault: t("setup.chooseVault"), clients: t("setup.installClients"),
    sync: t(mode === "init" ? "setup.initialize" : "setup.acceptPair"), scheduler: t("setup.installScheduler"),
  };

  return <div className="section-stack">
    <Panel title={t("setup.title")}>
      <Stepper activeStep={active} alternativeLabel nonLinear className="setup-stepper">
        {steps.map((id) => <Step key={id} completed={done(id)}>
          <StepButton onClick={() => select(id)} disabled={busy} icon={
            <span className={`setup-step-icon ${id === current ? "is-active" : ""} ${done(id) ? "is-complete" : ""}`}>
              {id === "uv" && uvChecking || busy && id === current ? <CircularProgress size={14} /> :
                done(id) ? <CheckOutlined /> : steps.indexOf(id) + 1}
            </span>}>{t(`setup.step.${id}`)}</StepButton>
        </Step>)}
      </Stepper>
      {!current ? <div className="settings-row">
        <span>{t("setup.complete")}</span>
        <Button color="inherit" onClick={() => select("uv")}>{t("setup.reopen")}</Button>
      </div> : <div className="setup-content">
        <h3>{t(`setup.step.${current}`)}</h3>
        <p>{t(`setup.${current}.description`)}</p>
        {current === "uv" && uv?.available && <p className="mono meta">{uv.version} · {uv.path}</p>}
        {current === "vault" && vault && <p><span className="meta">{t("setup.selectedVault")}</span> <span className="mono">{vault}</span></p>}
        {current === "sync" && <div className="setup-remote">
          <RadioGroup row value={mode} onChange={(event) => setMode(event.target.value === "pair" ? "pair" : "init")}>
            <FormControlLabel value="init" control={<Radio size="small" />} label={t("setup.initialize")} />
            <FormControlLabel value="pair" control={<Radio size="small" />} label={t("setup.acceptPair")} />
          </RadioGroup>
          <p className="meta">{t(mode === "init" ? "setup.initializeHint" : "setup.pairHint")}</p>
          <TextField label={t("setup.remoteLabel")} placeholder={t("setup.remotePlaceholder")} value={remote}
            onChange={(event) => setRemote(event.target.value)} fullWidth slotProps={{ htmlInput: { maxLength: 512 } }} />
        </div>}
        <div className="meta">{t("setup.command")}</div>
        <pre className="setup-command mono">{commandFor(current, vault, remote, mode)}</pre>
        {result && <Alert severity={result.ok ? "success" : "error"}>{result.message}</Alert>}
        <div className="setup-actions">
          <Button color="inherit" disabled={active === 0 || busy} onClick={() => select(steps[active - 1])}>{t("common.back")}</Button>
          <Button variant="outlined" color="inherit" startIcon={busy ? <CircularProgress size={16} /> : <PlayArrowOutlined />}
            onClick={() => void runCurrent()} disabled={busy || (current === "clients" && !vault)}>{busy ? t("common.loading") : actionLabel[current]}</Button>
        </div>
      </div>}
    </Panel>
    <Panel title={t("settings.app")}>
      <div className="settings-row">
        <label htmlFor="login-at-start">{t("settings.login")}</label>
        <Switch checked={login ?? false} disabled={login === undefined || loginBusy} onChange={(_, enabled) => void updateLogin(enabled)}
          slotProps={{ input: { id: "login-at-start" } }} />
      </div>
      {loginError && <p role="alert" className="login-error">{loginError}</p>}
      <div className="settings-row"><span>{t("settings.log")}</span><Button color="inherit" variant="outlined" onClick={() => void runAction(api.openLog)}>{t("actions.openLog")}</Button></div>
    </Panel>
  </div>;
}
