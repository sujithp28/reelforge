/**
 * Save a finished reel into the Downloads folder.
 *
 * Pure helpers live here so they can be tested without Electron. The main
 * process supplies the Downloads directory from app.getPath("downloads").
 */
const fs = require("fs");
const path = require("path");

const PROJECT_ID = /^proj_[a-z0-9]+$/;
const EXPORT_FILENAME = /^ReelForge-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-\d{8}(?:-\d+)?\.mp4$/;

function todayStamp(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}${month}${day}`;
}

function exportBaseName(title, stamp) {
  const words = String(title || "")
    .normalize("NFKD")
    .replace(/[^A-Za-z0-9]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 8)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase());
  const slug = words.join("-").slice(0, 60) || "Reel";
  return `ReelForge-${slug}-${stamp}`;
}

function uniqueExportPath(directory, baseName, exists) {
  const first = path.join(directory, `${baseName}.mp4`);
  if (!exists(first)) return first;
  for (let index = 1; index < 1000; index += 1) {
    const candidate = path.join(directory, `${baseName}-${index}.mp4`);
    if (!exists(candidate)) return candidate;
  }
  const error = new Error("too many exports");
  error.code = "EEXIST";
  throw error;
}

function resolveDownloadsDir(getPath) {
  if (typeof getPath !== "function") {
    throw Object.assign(new Error("downloads unavailable"), { code: "ENOENT" });
  }
  const dir = getPath("downloads");
  if (typeof dir !== "string" || !dir.trim()) {
    throw Object.assign(new Error("downloads unavailable"), { code: "ENOENT" });
  }
  return path.resolve(dir);
}

function isCompleteMp4(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length < 1024) return false;
  const head = bytes.subarray(0, 64).toString("latin1");
  if (!head.includes("ftyp")) return false;
  return bytes.toString("latin1").includes("moov");
}

function validateSaveRequest(payload) {
  if (!payload || typeof payload !== "object") {
    return { ok: false, message: "That reel could not be saved." };
  }
  const projectId = payload.projectId;
  if (typeof projectId !== "string" || !PROJECT_ID.test(projectId)) {
    return { ok: false, message: "That reel could not be saved." };
  }
  return { ok: true, projectId };
}

function mediaPathForProject(videoUrl, projectId) {
  if (typeof videoUrl !== "string" || typeof projectId !== "string") return null;
  let pathname = videoUrl;
  if (videoUrl.startsWith("http://") || videoUrl.startsWith("https://")) {
    let url;
    try {
      url = new URL(videoUrl);
    } catch {
      return null;
    }
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") return null;
    pathname = url.pathname;
  }
  if (pathname.includes("..") || pathname.includes("\\")) return null;
  const expected = `/media/renders/${projectId}.mp4`;
  return pathname === expected ? pathname : null;
}

function exportFileInDownloads(downloadsDir, filename) {
  if (typeof filename !== "string" || !EXPORT_FILENAME.test(filename)) return null;
  if (filename.includes("..") || filename.includes("/") || filename.includes("\\")) return null;
  const root = path.resolve(downloadsDir);
  const full = path.resolve(root, filename);
  const relative = path.relative(root, full);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) return null;
  return full;
}

function customerSaveError(error) {
  const code = error && error.code;
  if (code === "EACCES" || code === "EPERM") {
    return "ReelForge could not save the reel. Check that Downloads is writable.";
  }
  if (code === "ENOSPC") {
    return "ReelForge could not save the reel because the disk is full.";
  }
  return "ReelForge could not save the reel to Downloads.";
}

async function publishExport({ bytes, directory, baseName, io }) {
  const files = io || fs;
  if (!isCompleteMp4(bytes)) {
    return { ok: false, message: "The reel was not saved because the file was incomplete." };
  }
  let target;
  try {
    target = uniqueExportPath(directory, baseName, (candidate) => files.existsSync(candidate));
  } catch (error) {
    return { ok: false, message: customerSaveError(error) };
  }
  const partial = `${target}.partial`;
  try {
    await files.promises.writeFile(partial, bytes);
    await files.promises.rename(partial, target);
  } catch (error) {
    await files.promises.unlink(partial).catch(() => {});
    return { ok: false, message: customerSaveError(error) };
  }
  return { ok: true, filename: path.basename(target) };
}

module.exports = {
  EXPORT_FILENAME,
  todayStamp,
  exportBaseName,
  uniqueExportPath,
  resolveDownloadsDir,
  isCompleteMp4,
  validateSaveRequest,
  mediaPathForProject,
  exportFileInDownloads,
  customerSaveError,
  publishExport,
};
