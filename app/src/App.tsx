import { useCallback, useEffect, useState } from "react";
import { CssBaseline, ThemeProvider } from "@mui/material";
import type { BinderySnapshot } from "./types";
import { theme } from "./theme";
import { PopoverView } from "./components/PopoverView";
import { SettingsView } from "./components/SettingsView";

export default function App() {
  const kind = window.bindery.getWindowKind();
  const [snapshot, setSnapshot] = useState<BinderySnapshot | null>(null);

  const refresh = useCallback(async () => {
    setSnapshot(await window.bindery.getSnapshot());
  }, []);

  useEffect(() => {
    if (kind !== "popover") return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10_000);
    return () => window.clearInterval(timer);
  }, [kind, refresh]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {kind === "settings" ? <SettingsView /> : <PopoverView snapshot={snapshot} onRefresh={refresh} />}
    </ThemeProvider>
  );
}
