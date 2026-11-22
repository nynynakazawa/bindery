import { contextBridge, ipcRenderer } from "electron";
import { IPC_CHANNELS } from "./security";

const windowKind = process.argv.includes("--bindery-window=settings") ? "settings" : "popover";

contextBridge.exposeInMainWorld("bindery", {
  getWindowKind: () => windowKind,
  getSnapshot: () => ipcRenderer.invoke(IPC_CHANNELS.getSnapshot),
  syncNow: () => ipcRenderer.invoke(IPC_CHANNELS.syncNow),
  openVault: () => ipcRenderer.invoke(IPC_CHANNELS.openVault),
  openLog: () => ipcRenderer.invoke(IPC_CHANNELS.openLog),
  showSettings: () => ipcRenderer.invoke(IPC_CHANNELS.showSettings),
  checkUv: () => ipcRenderer.invoke(IPC_CHANNELS.checkUv),
  installWheel: () => ipcRenderer.invoke(IPC_CHANNELS.installWheel),
  selectVault: () => ipcRenderer.invoke(IPC_CHANNELS.selectVault),
  installClients: () => ipcRenderer.invoke(IPC_CHANNELS.installClients),
  writeGlobalPrompt: () => ipcRenderer.invoke(IPC_CHANNELS.writeGlobalPrompt),
  initSync: (remote: string) => ipcRenderer.invoke(IPC_CHANNELS.initSync, remote),
  pairSync: (remote: string) => ipcRenderer.invoke(IPC_CHANNELS.pairSync, remote),
  installScheduler: () => ipcRenderer.invoke(IPC_CHANNELS.installScheduler),
});
