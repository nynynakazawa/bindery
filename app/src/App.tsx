import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, lazy, Suspense } from "react";
import { Button, ButtonBase, CircularProgress, CssBaseline, IconButton, Snackbar, ThemeProvider, Tooltip } from "@mui/material";
import DashboardOutlined from "@mui/icons-material/DashboardOutlined";
import CableOutlined from "@mui/icons-material/CableOutlined";
import SyncOutlined from "@mui/icons-material/SyncOutlined";
import AutoStoriesOutlined from "@mui/icons-material/AutoStoriesOutlined";
import TableRowsOutlined from "@mui/icons-material/TableRowsOutlined";
import TuneOutlined from "@mui/icons-material/TuneOutlined";
import RefreshOutlined from "@mui/icons-material/RefreshOutlined";
import type { CommandResult } from "./types";
import { t } from "./copy";
import { theme } from "./theme";
import { SettingsView } from "./components/SettingsView";
import { OverviewView } from "./components/OverviewView";
import { ConnectionsView, SyncView } from "./components/OperationsViews";
import { MemoryView } from "./components/MemoryView";
import { IndexView } from "./components/IndexView";
import { LoadingRows, relativeTime, SyncIndicator, syncPresentation } from "./components/DashboardParts";
import { useDashboardData } from "./lib/dashboardData";
import type { NoteLocation } from "./lib/noteLinks";
import "./dashboard.css";

const NoteViewer = lazy(() => import("./components/NoteViewer").then(({ NoteViewer }) => ({ default: NoteViewer })));

const nav = {
  overview: DashboardOutlined,
  connections: CableOutlined,
  sync: SyncOutlined,
  memory: AutoStoriesOutlined,
  index: TableRowsOutlined,
  settings: TuneOutlined,
};
type Page = keyof typeof nav;
const emptyHistory: { entries: NoteLocation[]; position: number } = { entries: [], position: -1 };
const noPaths: string[] = [];

export default function App() {
  const api = window.bindery;
  const snapshot = useDashboardData(api.getSnapshot);
  const recent = useDashboardData(api.recent);
  const rankings = useDashboardData(api.ranking);
  const notes = useDashboardData(api.listNotes);
  const refreshSnapshot = snapshot.refresh;
  const refreshRecent = recent.refresh;
  const refreshRankings = rankings.refresh;
  const refreshNotes = notes.refresh;
  const [route, setRoute] = useState<{ page: Page; section?: string }>({ page: "overview" });
  const { page } = route;
  const [settingsVisited, setSettingsVisited] = useState(false);
  const [history, setHistory] = useState(emptyHistory);
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncError, setSyncError] = useState<string>();
  const [registerBusy, setRegisterBusy] = useState(false);
  const [registerError, setRegisterError] = useState<string>();
  const [toast, setToast] = useState<string>();
  const [scrolled, setScrolled] = useState(false);
  const localSync = useRef(false);
  const scrollArea = useRef<HTMLDivElement>(null);
  const lastSync = useRef<string | undefined>(undefined);
  const focusSearch = useRef(false);
  const snapshotData = snapshot.data;
  const sync = snapshotData?.sync;
  const syncing = syncBusy || Boolean(snapshotData?.syncing);
  const syncState = syncPresentation(sync, false);
  const location = history.entries[history.position];

  const closeNote = useCallback(() => setHistory(emptyHistory), []);
  const openNote = useCallback((note: NoteLocation) => {
    setHistory(({ entries, position }) => {
      const current = entries[position];
      if (current?.path === note.path && current.heading === note.heading) return { entries, position };
      return { entries: [...entries.slice(0, position + 1), note], position: position + 1 };
    });
  }, []);

  const go = useCallback((next: Page, section?: string) => {
    if (next !== page) closeNote();
    if (next === "settings") setSettingsVisited(true);
    if (next !== page || section) setRoute({ page: next, section });
  }, [closeNote, page]);

  const run = useCallback(async (action: () => Promise<CommandResult | void>) => {
    try {
      const result = await action();
      if (result && !result.ok) setToast(result.message || t("common.actionFailed"));
    } catch (error) {
      setToast(error instanceof Error ? error.message : t("common.actionFailed"));
    }
  }, []);

  const refreshLists = useCallback((force = false, supersede = false) => Promise.all([
    refreshNotes(force, supersede), refreshRecent(force, supersede), refreshRankings(force, supersede),
  ]), [refreshNotes, refreshRankings, refreshRecent]);

  useEffect(() => {
    void refreshSnapshot();
    void refreshLists();
    const timer = setInterval(() => {
      if (!localSync.current) void refreshSnapshot(true);
    }, 10_000);
    return () => clearInterval(timer);
  }, [refreshLists, refreshSnapshot]);

  useEffect(() => {
    if (page === "overview" || page === "memory" || page === "index" || page === "sync") {
      void refreshLists();
    }
  }, [page, refreshLists]);

  useEffect(() => {
    if (!snapshotData || snapshotData.syncing) return;
    const stamp = JSON.stringify([snapshotData.status.vault, snapshotData.sync.last_attempt, snapshotData.sync.last_success]);
    if (!localSync.current && lastSync.current !== undefined && stamp !== lastSync.current) {
      void refreshLists(true, true);
    }
    lastSync.current = stamp;
  }, [snapshotData, refreshLists]);

  const paths = useMemo(() => notes.data?.error === null ? notes.data.notes.map((note) => note.path) : noPaths, [notes.data]);

  useLayoutEffect(() => {
    const main = scrollArea.current;
    if (!main) return;
    if (route.section) {
      const target = document.getElementById(route.section);
      const topOffset = page === "memory" ? 60 : 20;
      main.scrollTop = target ? target.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop - topOffset : 0;
    } else {
      main.scrollTop = 0;
    }
    setScrolled(main.scrollTop >= 8);
    if (focusSearch.current && page === "memory") {
      focusSearch.current = false;
      document.getElementById("memory-search-input")?.focus();
    }
  }, [route, page]);

  useEffect(() => {
    function onShortcut(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k" || event.defaultPrevented) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      event.preventDefault();
      if (page === "memory") document.getElementById("memory-search-input")?.focus();
      else {
        focusSearch.current = true;
        go("memory");
      }
    }
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [go, page]);

  async function syncNow() {
    if (localSync.current || syncing) return;
    localSync.current = true;
    setSyncBusy(true);
    setSyncError(undefined);
    try {
      const result = await api.syncNow();
      lastSync.current = JSON.stringify([result.status.vault, result.sync.last_attempt, result.sync.last_success]);
      snapshot.replace(result);
      void refreshLists(true, true);
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : t("sync.loadError"));
    } finally {
      localSync.current = false;
      setSyncBusy(false);
    }
  }

  async function register() {
    setRegisterBusy(true);
    setRegisterError(undefined);
    try {
      if (!snapshotData?.status.vault_exists && !await api.selectVault()) return;
      const result = await api.installClients();
      if (!result.ok) setRegisterError(result.message);
      else {
        await refreshSnapshot(true, true);
        void refreshLists(true, true);
        setToast(t("clients.updated"));
      }
    } catch (error) {
      setRegisterError(error instanceof Error ? error.message : t("clients.registerError"));
    } finally {
      setRegisterBusy(false);
    }
  }

  const reload = () => {
    void refreshSnapshot(true);
    void refreshLists(true);
  };

  const setupChanged = () => {
    void refreshSnapshot(true, true);
    void refreshLists(true, true);
  };

  const rankingState = { ...rankings, data: rankings.data?.error === null ? rankings.data : undefined };
  const recentState = { ...recent, data: recent.data?.error === null ? recent.data : undefined };
  const notesState = { ...notes, data: notes.data?.error === null ? notes.data : undefined };
  const syncButton = <Button variant="outlined" color="inherit" disabled={syncing || !sync?.configured} onClick={() => void syncNow()}
    startIcon={syncing ? <CircularProgress size={16} /> : <SyncOutlined />}>{t(syncing ? "sync.running" : "actions.syncNow")}</Button>;

  return <ThemeProvider theme={theme}>
    <CssBaseline />
    <div className="dashboard">
      <aside className="sidebar">
        <div className="sidebar-drag" aria-hidden="true" />
        <nav aria-label={t("app.name")}>
          {(Object.keys(nav) as Page[]).map((id) => {
            const Icon = nav[id];
            return <ButtonBase key={id} className="nav-item" aria-current={page === id ? "page" : undefined}
              onClick={() => go(id)}>
              <Icon /><span>{t(`nav.${id}`)}</span>
              {id === "sync" && syncState.attention && <span className="nav-dot" style={{ backgroundColor: syncState.color }} aria-label={syncState.label} />}
            </ButtonBase>;
          })}
        </nav>
        <ButtonBase className="sidebar-footer" onClick={() => go("sync")}>
          <SyncIndicator sync={sync} running={syncing} />
          {sync?.last_success && <span className="meta ellipsis">{relativeTime(sync.last_success)}</span>}
        </ButtonBase>
      </aside>
      <main className="dashboard-main">
        <header className={`dashboard-header ${scrolled ? "scrolled" : ""}`}>
          <h1>{t(`nav.${page}`)}</h1>
          <div className="header-actions">
            {(page === "overview" || page === "sync") && syncButton}
            {page === "connections" && <Button variant="outlined" color="inherit" disabled={registerBusy || !snapshotData || Boolean(snapshotData.clients.error)}
              startIcon={registerBusy ? <CircularProgress size={16} /> : <CableOutlined />} onClick={() => void register()}>
              {t(registerBusy ? "actions.registering" : "actions.register")}
            </Button>}
            {(page === "index" || page === "memory") && <Tooltip title={t("common.reload")}>
              <IconButton aria-label={t("common.reload")} onClick={reload}><RefreshOutlined /></IconButton>
            </Tooltip>}
          </div>
        </header>
        <div className="dashboard-scroll" ref={scrollArea} onScroll={(event) => setScrolled(event.currentTarget.scrollTop >= 8)}>
          <div className="dashboard-content">
            {page === "overview" && <OverviewView snapshot={snapshotData} rankings={rankingState} recent={recentState} syncing={syncing}
              go={go} open={openNote} refresh={reload} error={syncError ?? snapshot.error} />}
            {page === "connections" && <ConnectionsView snapshot={snapshotData} register={() => void register()}
              registerError={registerError ?? snapshot.error} refresh={reload} run={run} />}
            {page === "sync" && <SyncView snapshot={snapshotData} syncing={syncing} error={syncError ?? snapshot.error}
              retry={() => void syncNow()} refresh={reload} goSettings={() => go("settings")} open={openNote} run={run} />}
            {page === "memory" && <MemoryView rankings={rankingState} recent={recentState} open={openNote} />}
            {page === "index" && <IndexView source={notesState} activePath={location?.path} open={openNote} />}
            {settingsVisited && <div hidden={page !== "settings"}><SettingsView snapshot={snapshotData} onChanged={setupChanged} runAction={run} /></div>}
          </div>
        </div>
        {location && <Suspense fallback={<aside className="note-viewer" aria-label={t("note.viewer")}><div className="viewer-body"><LoadingRows count={7} /></div></aside>}><NoteViewer location={location} paths={paths} open={openNote} close={closeNote}
          back={history.position > 0 ? () => setHistory((value) => ({ ...value, position: value.position - 1 })) : undefined}
          forward={history.position < history.entries.length - 1 ? () => setHistory((value) => ({ ...value, position: value.position + 1 })) : undefined}
          run={run} /></Suspense>}
      </main>
      <Snackbar open={Boolean(toast)} message={toast} autoHideDuration={4000} onClose={() => setToast(undefined)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }} />
    </div>
  </ThemeProvider>;
}
