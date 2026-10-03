/**
 * The only bridge the page receives. Node and the filesystem stay in main.
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("reelforgeDesktop", {
  saveExport: (payload) => ipcRenderer.invoke("reelforge:save-export", payload),
  revealExport: (filename) => ipcRenderer.invoke("reelforge:reveal-export", filename),
  openDownloads: () => ipcRenderer.invoke("reelforge:open-downloads"),
});
