/**
 * Downloads export checks. Run from the repository root:
 *   node desktop/test-export-file.js
 */
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const exp = require("./export-file");

function fakeMp4() {
  const bytes = Buffer.alloc(2048, 0);
  bytes.write("ftypisom", 4);
  bytes.write("moov", 1500);
  return bytes;
}

function testDownloadsDirComesFromTheCaller() {
  let asked = null;
  const dir = exp.resolveDownloadsDir((key) => {
    asked = key;
    return path.join(os.tmpdir(), "reelforge-downloads-check");
  });
  assert.strictEqual(asked, "downloads");
  assert.strictEqual(dir, path.resolve(path.join(os.tmpdir(), "reelforge-downloads-check")));
  assert.throws(() => exp.resolveDownloadsDir(() => ""), /downloads unavailable/);
  assert.throws(() => exp.resolveDownloadsDir(null), /downloads unavailable/);
}

function testFilename() {
  assert.strictEqual(
    exp.exportBaseName("Luxury Interior", "20261001"),
    "ReelForge-Luxury-Interior-20261001",
  );
  assert.strictEqual(exp.todayStamp(new Date(2026, 9, 1)), "20261001");
  assert.strictEqual(exp.exportBaseName("  ", "20261001"), "ReelForge-Reel-20261001");
}

function testCollisionsLeaveTheExistingFileAlone() {
  const directory = path.join(os.tmpdir(), "reelforge-name-check");
  const base = "ReelForge-Luxury-Interior-20261001";
  const first = path.join(directory, `${base}.mp4`);
  const second = path.join(directory, `${base}-1.mp4`);
  const chosen = exp.uniqueExportPath(directory, base, (candidate) => candidate === first);
  assert.strictEqual(chosen, second);
  const fresh = exp.uniqueExportPath(directory, base, () => false);
  assert.strictEqual(fresh, first);
}

async function testSuccessfulSaveAndFailedSave() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "reelforge-export-"));
  const base = "ReelForge-Luxury-Interior-20261001";
  const previous = path.join(directory, `${base}.mp4`);
  fs.writeFileSync(previous, Buffer.from("previous-good-reel"));

  const saved = await exp.publishExport({
    bytes: fakeMp4(),
    directory,
    baseName: base,
  });
  assert.strictEqual(saved.ok, true);
  assert.strictEqual(saved.filename, `${base}-1.mp4`);
  assert.strictEqual(fs.readFileSync(previous).toString(), "previous-good-reel");
  assert.ok(exp.isCompleteMp4(fs.readFileSync(path.join(directory, saved.filename))));
  assert.ok(!fs.existsSync(path.join(directory, `${saved.filename}.partial`)));

  const incomplete = await exp.publishExport({
    bytes: Buffer.from("not-a-reel"),
    directory,
    baseName: base,
    io: {
      existsSync: () => { throw new Error("should not look for a name"); },
      promises: { writeFile: async () => {}, rename: async () => {}, unlink: async () => {} },
    },
  });
  assert.strictEqual(incomplete.ok, false);
  assert.match(incomplete.message, /incomplete/);
  assert.strictEqual(fs.readFileSync(previous).toString(), "previous-good-reel");

  let removedPartial = false;
  const failed = await exp.publishExport({
    bytes: fakeMp4(),
    directory,
    baseName: "ReelForge-Other-20261001",
    io: {
      existsSync: () => false,
      promises: {
        writeFile: async () => {
          const error = new Error("full");
          error.code = "ENOSPC";
          throw error;
        },
        rename: async () => {},
        unlink: async () => { removedPartial = true; },
      },
    },
  });
  assert.strictEqual(failed.ok, false);
  assert.match(failed.message, /disk is full/);
  assert.strictEqual(removedPartial, true);
  assert.strictEqual(fs.readFileSync(previous).toString(), "previous-good-reel");
  fs.rmSync(directory, { recursive: true, force: true });
}

function testIpcValidation() {
  assert.strictEqual(exp.validateSaveRequest({ projectId: "proj_abc123" }).ok, true);
  assert.strictEqual(exp.validateSaveRequest({ projectId: "../renders/x" }).ok, false);
  assert.strictEqual(exp.validateSaveRequest({ projectId: "proj_abc/../../secret" }).ok, false);
  assert.strictEqual(exp.validateSaveRequest(null).ok, false);
  assert.strictEqual(
    exp.mediaPathForProject("/media/renders/proj_abc123.mp4", "proj_abc123"),
    "/media/renders/proj_abc123.mp4",
  );
  assert.strictEqual(
    exp.mediaPathForProject("http://127.0.0.1:8000/media/renders/proj_abc123.mp4", "proj_abc123"),
    "/media/renders/proj_abc123.mp4",
  );
  assert.strictEqual(
    exp.mediaPathForProject("http://example.com/media/renders/proj_abc123.mp4", "proj_abc123"),
    null,
  );
  assert.strictEqual(
    exp.mediaPathForProject("/media/renders/proj_other.mp4", "proj_abc123"),
    null,
  );
  assert.strictEqual(exp.mediaPathForProject("/media/../reelforge.db", "proj_abc123"), null);

  const downloads = path.join(os.tmpdir(), "reelforge-downloads");
  const revealed = exp.exportFileInDownloads(downloads, "ReelForge-Luxury-Interior-20261001-1.mp4");
  assert.ok(revealed && revealed.endsWith("ReelForge-Luxury-Interior-20261001-1.mp4"));
  assert.strictEqual(exp.exportFileInDownloads(downloads, "..\\secret.mp4"), null);
  assert.strictEqual(exp.exportFileInDownloads(downloads, "C:\\Windows\\notepad.exe"), null);
}

async function main() {
  testDownloadsDirComesFromTheCaller();
  testFilename();
  testCollisionsLeaveTheExistingFileAlone();
  await testSuccessfulSaveAndFailedSave();
  testIpcValidation();
  console.log("desktop export checks passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
