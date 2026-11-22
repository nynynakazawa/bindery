import { contextBridge, ipcRenderer } from "electron";
import { IPC_CHANNELS } from "./channels";
import type { BinderyApi } from "./types";

contextBridge.exposeInMainWorld("bindery", {
  getSnapshot: () => ipcRenderer.invoke(IPC_CHANNELS.getSnapshot),
  syncNow: () => ipcRenderer.invoke(IPC_CHANNELS.syncNow),
  openVault: () => ipcRenderer.invoke(IPC_CHANNELS.openVault),
  openLog: () => ipcRenderer.invoke(IPC_CHANNELS.openLog),
  checkUv: () => ipcRenderer.invoke(IPC_CHANNELS.checkUv),
  installWheel: () => ipcRenderer.invoke(IPC_CHANNELS.installWheel),
  selectVault: () => ipcRenderer.invoke(IPC_CHANNELS.selectVault),
  installClients: () => ipcRenderer.invoke(IPC_CHANNELS.installClients),
  writeGlobalPrompt: () => ipcRenderer.invoke(IPC_CHANNELS.writeGlobalPrompt),
  initSync: (remote: string) => ipcRenderer.invoke(IPC_CHANNELS.initSync, remote),
  pairSync: (remote: string) => ipcRenderer.invoke(IPC_CHANNELS.pairSync, remote),
  installScheduler: () => ipcRenderer.invoke(IPC_CHANNELS.installScheduler),
  search: (query) => ipcRenderer.invoke(IPC_CHANNELS.search, query),
  listNotes: () => ipcRenderer.invoke(IPC_CHANNELS.listNotes),
  readNote: (relative) => ipcRenderer.invoke(IPC_CHANNELS.readNote, relative),
  recent: () => ipcRenderer.invoke(IPC_CHANNELS.recent),
  ranking: () => ipcRenderer.invoke(IPC_CHANNELS.ranking),
  openInObsidian: (relative) => ipcRenderer.invoke(IPC_CHANNELS.openInObsidian, relative),
  revealInFinder: (relative) => ipcRenderer.invoke(IPC_CHANNELS.revealInFinder, relative),
  openExternal: (url) => ipcRenderer.invoke(IPC_CHANNELS.openExternal, url),
  setLoginItem: (enabled) => ipcRenderer.invoke(IPC_CHANNELS.setLoginItem, enabled),
  getLoginItem: () => ipcRenderer.invoke(IPC_CHANNELS.getLoginItem),
} satisfies BinderyApi);
