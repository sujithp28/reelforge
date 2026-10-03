/**
 * ReelForge Windows shell.
 *
 * Starts the local API, then opens the web app. Paths come from the
 * environment or from Electron's per-user data folder. Nothing here is tied
 * to a developer's machine.
 *
 * The web app is unchanged. This process does not render video itself.
 */
const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const exportFile = require("./export-file");

const API_HOST = "127.0.0.1";
const API_PORT = String(process.env.REELFORGE_PORT || "8000");
const WEB_URL = process.env.REELFORGE_WEB_URL || "http://127.0.0.1:3000";

let backend = null;
let windowRef = null;

function repoRoot() {
  if (process.env.REELFORGE_ROOT) return process.env.REELFORGE_ROOT;
  return path.resolve(__dirname, "..");
}

function startBackend() {
  const python = process.env.REELFORGE_PYTHON || "python";
  const dataDir = path.join(app.getPath("userData"), "data");
  const env = { ...process.env, REELFORGE_DATA_DIR: dataDir };
  backend = spawn(
    python,
    ["-m", "uvicorn", "app.main:app", "--host", API_HOST, "--port", API_PORT],
    {
      cwd: path.join(repoRoot(), "backend"),
      env,
      windowsHide: true,
    },
  );
  backend.on("error", () => {
    dialog.showErrorBox(
      "ReelForge could not start",
      "The local service did not start. Check the ReelForge setup, then open it again.",
    );
  });
}

function createWindow() {
  windowRef = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 360,
    minHeight: 640,
    title: "ReelForge",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  windowRef.loadURL(WEB_URL).catch(() => {
    dialog.showErrorBox(
      "ReelForge could not open",
      "The window could not load. Start the site first, or set REELFORGE_WEB_URL.",
    );
  });
}

function apiBase() {
  return `http://${API_HOST}:${API_PORT}`;
}

async function readFinishedReel(projectId) {
  const response = await fetch(`${apiBase()}/api/projects/${projectId}`);
  if (!response.ok) return { ok: false, message: "That reel could not be saved." };
  const project = await response.json();
  if (project.status !== "ready" || project.output_stale) {
    return { ok: false, message: "Save the reel after it finishes rendering." };
  }
  const mediaPath = exportFile.mediaPathForProject(project.video_url, projectId);
  if (!mediaPath) return { ok: false, message: "That reel could not be saved." };
  const file = await fetch(`${apiBase()}${mediaPath}`);
  if (!file.ok) return { ok: false, message: "That reel could not be saved." };
  return {
    ok: true,
    bytes: Buffer.from(await file.arrayBuffer()),
    title: typeof project.title === "string" ? project.title : "",
  };
}

ipcMain.handle("reelforge:save-export", async (_event, payload) => {
  const checked = exportFile.validateSaveRequest(payload);
  if (!checked.ok) return checked;
  try {
    const reel = await readFinishedReel(checked.projectId);
    if (!reel.ok) return reel;
    const directory = exportFile.resolveDownloadsDir((name) => app.getPath(name));
    return await exportFile.publishExport({
      bytes: reel.bytes,
      directory,
      baseName: exportFile.exportBaseName(reel.title, exportFile.todayStamp()),
    });
  } catch (error) {
    return { ok: false, message: exportFile.customerSaveError(error) };
  }
});

ipcMain.handle("reelforge:reveal-export", async (_event, filename) => {
  try {
    const directory = exportFile.resolveDownloadsDir((name) => app.getPath(name));
    const full = exportFile.exportFileInDownloads(directory, filename);
    if (!full || !fs.existsSync(full)) {
      return { ok: false, message: "That saved reel could not be found." };
    }
    shell.showItemInFolder(full);
    return { ok: true };
  } catch {
    return { ok: false, message: "The Downloads folder could not be opened." };
  }
});

ipcMain.handle("reelforge:open-downloads", async () => {
  try {
    const directory = exportFile.resolveDownloadsDir((name) => app.getPath(name));
    const problem = await shell.openPath(directory);
    if (problem) return { ok: false, message: "The Downloads folder could not be opened." };
    return { ok: true };
  } catch {
    return { ok: false, message: "The Downloads folder could not be opened." };
  }
});

app.whenReady().then(() => {
  if (process.env.REELFORGE_DESKTOP_API !== "external") startBackend();
  createWindow();
});

app.on("window-all-closed", () => {
  if (backend && !backend.killed) backend.kill();
  app.quit();
});

app.on("before-quit", () => {
  if (backend && !backend.killed) backend.kill();
});
