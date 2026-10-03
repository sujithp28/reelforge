"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  AlertTriangle, ArrowLeft, Check, ChevronDown, ChevronUp, Download, Eye, FolderOpen, ImagePlus,
  Loader2, Music, Pause, Play, RefreshCw, RotateCcw, Trash2, WandSparkles, X,
} from "lucide-react";
import {
  api, EXPORT_FORMATS, LUXURY_CATEGORY, MOCK_QUALITY_NOTE, QUALITIES, exportFormatLabel, mediaUrl, selectedExportFormat,
  type Asset, type ExportFormat, type Project, type Quality, type Scene,
} from "../../../lib/api";
import { desktopBridge } from "../../../lib/desktop";
import { LuxuryStudio } from "../../../components/LuxuryStudio";

const WORKFLOW = [
  "Create a project",
  "Upload images",
  "Organize scenes",
  "Add titles and subtitles",
  "Configure music",
  "Configure export",
  "Render",
  "Preview",
  "Download",
];

const RATIO_CLASS: Record<string, string> = {
  "9:16": "aspect-[9/16]",
  "16:9": "aspect-video",
  "1:1": "aspect-square",
  "4:5": "aspect-[4/5]",
};

function luxuryImageGap(project: Project): string | null {
  if (project.category !== LUXURY_CATEGORY) return null;
  const missing = project.scenes.flatMap((scene, index) => scene.asset_id ? [] : [index + 1]);
  if (missing.length === 0) return null;
  const listed = missing.length === 1 ? `Scene ${missing[0]}` : `Scenes ${missing.join(", ")}`;
  const verb = missing.length === 1 ? "needs" : "need";
  return `${listed} still ${verb} an image. Upload or assign a photo to each scene before rendering.`;
}

/** Saved name for the finished reel. The browser chooses the folder. */
function reelDownloadName(title: string): string {
  const name = title
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80);
  return `reelforge-${name || "reel"}.mp4`;
}

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [selectedAsset, setSelectedAsset] = useState<string | null>(null);
  const [previewScene, setPreviewScene] = useState<Scene | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [exportPhase, setExportPhase] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const [exportName, setExportName] = useState<string | null>(null);
  const [exportNote, setExportNote] = useState<string | null>(null);
  const sawRendering = useRef(false);
  const [videoProvider, setVideoProvider] = useState<string | null>(null);
  const mockPreview = videoProvider === "mock";

  const load = useCallback(async () => {
    try {
      setProject(await api.getProject(id));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load this reel.");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    api.health()
      .then(body => {
        if (!cancelled) setVideoProvider(body.video_provider);
      })
      .catch(() => {
        if (!cancelled) setVideoProvider(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll only while a render is actually in flight.
  useEffect(() => {
    if (project?.status !== "rendering") return;
    sawRendering.current = true;
    const timer = setInterval(() => void load(), 1500);
    return () => clearInterval(timer);
  }, [project?.status, load]);

  useEffect(() => {
    if (!sawRendering.current || !project) return;
    if (project.status !== "ready" || project.output_stale || !project.video_url) return;
    const desk = desktopBridge();
    if (!desk) return;
    sawRendering.current = false;
    let cancelled = false;
    setExportPhase("saving");
    setExportNote(null);
    void desk.saveExport({ projectId: project.id }).then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setExportPhase("saved");
        setExportName(result.filename);
        setExportNote(null);
      } else {
        setExportPhase("failed");
        setExportNote(result.message);
      }
    });
    return () => { cancelled = true; };
  }, [project]);

  async function act(key: string, fn: () => Promise<Project | unknown>) {
    setPending(key);
    setError(null);
    try {
      const next = await fn();
      if (next && typeof next === "object" && "scenes" in next) setProject(next as Project);
      else await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "That didn't work.");
    } finally {
      setPending(null);
    }
  }

  if (error && !project) {
    return (
      <main className="min-h-screen bg-zinc-950 px-6 py-16">
        <div className="mx-auto max-w-lg text-center">
          <AlertTriangle className="mx-auto text-amber-400" />
          <h1 className="mt-5 text-xl font-semibold">{error}</h1>
          <Link href="/dashboard" className="mt-6 inline-block rounded-xl bg-white px-5 py-3 text-sm font-semibold text-zinc-950">Back to dashboard</Link>
        </div>
      </main>
    );
  }

  if (!project) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-zinc-950 text-zinc-500">
        <Loader2 className="animate-spin" />
      </main>
    );
  }

  const reel = project;
  const videoUrl = mediaUrl(reel.video_url);
  const music = project.assets.find(a => a.kind === "audio");
  const downloadName = reelDownloadName(project.title);
  const images = project.assets.filter(asset => asset.kind === "image");
  const format = EXPORT_FORMATS.find(item => item.id === selectedExportFormat(project))
    ?? (project.aspect_ratio === "1:1"
      ? { id: "", label: "Square", ratio: "1:1", width: 1080, height: 1080, fps: 30 }
      : undefined);
  const musicOn = Boolean(project.music_enabled && music);
  const imageGap = luxuryImageGap(project);

  async function addPhotos(list: File[]) {
    if (!list.length) return;
    setError(null);
    try {
      let latest: Project | null = null;
      for (let i = 0; i < list.length; i++) {
        setPending(`Uploading ${i + 1} of ${list.length}`);
        const luxuryReel = reel.category === LUXURY_CATEGORY;
        latest = await api.upload(reel.id, list[i], undefined, {
          replan: luxuryReel && i === list.length - 1,
          append: !luxuryReel,
        });
      }
      if (latest) setProject(latest);
    } catch (e) {
      setError(e instanceof Error ? e.message : "That photo could not be added.");
      await load();
    } finally {
      setPending(null);
    }
  }

  function moveScene(index: number, direction: -1 | 1) {
    const next = index + direction;
    if (next < 0 || next >= reel.scenes.length) return;
    const ids = reel.scenes.map(scene => scene.id);
    const swapped = [...ids];
    [swapped[index], swapped[next]] = [swapped[next], swapped[index]];
    void act("order", () => api.reorderScenes(reel.id, swapped));
  }

  const desktop = desktopBridge();

  async function saveToDownloads() {
    if (!desktop || reel.output_stale || reel.status !== "ready") return;
    setExportPhase("saving");
    setExportNote(null);
    const result = await desktop.saveExport({ projectId: reel.id });
    if (result.ok) {
      setExportPhase("saved");
      setExportName(result.filename);
    } else {
      setExportPhase("failed");
      setExportNote(result.message);
    }
  }

  async function downloadReel() {
    if (!videoUrl) return;
    setPending("download");
    setError(null);
    try {
      const response = await fetch(videoUrl);
      if (!response.ok) throw new Error("download failed");
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = downloadName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoke after the browser has started the save. Immediate revoke can cancel it.
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
    } catch {
      setError("The reel could not be downloaded. Please try again.");
    } finally {
      setPending(null);
    }
  }

  return (
    <main className="min-h-screen bg-zinc-950 px-4 py-8 sm:px-6">
      <div className="mx-auto max-w-6xl">
        <Link href="/dashboard" className="mb-8 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"><ArrowLeft size={16}/> Dashboard</Link>

        {!project.scenes.some(scene => scene.asset_url) && (
          <div className="relative mb-6 flex cursor-pointer items-center justify-center gap-2 rounded-2xl border border-dashed border-amber-700/80 bg-amber-950/30 p-4 text-sm text-amber-100">
            <ImagePlus size={16}/> Add a photo to build the reel. Without one, the reel is title cards.
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp,image/bmp,image/*"
              aria-label="Add a photo for every scene"
              className="absolute inset-0 z-10 cursor-pointer opacity-0"
              onChange={e => {
                const f = e.target.files?.[0];
                if (f) void act("photo", () => api.upload(reel.id, f));
                e.target.value = "";
              }}
            />
          </div>
        )}

        <ol className="mb-6 flex gap-2 overflow-x-auto pb-1 text-xs text-zinc-500">
          {WORKFLOW.map((step, index) => (
            <li key={step} className="shrink-0 rounded-full border border-zinc-800 px-3 py-1">
              {index + 1}. {step}
            </li>
          ))}
        </ol>

        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="text-sm text-violet-400">Storyboard</p>
            {renaming ? (
              <form
                className="mt-1 flex flex-wrap gap-2"
                onSubmit={e => {
                  e.preventDefault();
                  const title = nameDraft.trim();
                  if (!title) return;
                  setRenaming(false);
                  void act("rename", () => api.renameProject(reel.id, title));
                }}
              >
                <input
                  value={nameDraft}
                  onChange={e => setNameDraft(e.target.value)}
                  aria-label="Reel name"
                  maxLength={120}
                  className="w-full max-w-md rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-lg outline-none focus:border-violet-500 sm:w-80"
                />
                <button type="submit" className="rounded-lg bg-violet-500 px-3 py-2 text-sm font-semibold">Save</button>
                <button type="button" onClick={() => setRenaming(false)} className="rounded-lg border border-zinc-700 px-3 py-2 text-sm">Cancel</button>
              </form>
            ) : (
              <div className="mt-1 flex flex-wrap items-center gap-3">
                <h1 className="text-3xl font-semibold">{project.title}</h1>
                <button
                  type="button"
                  onClick={() => { setNameDraft(reel.title); setRenaming(true); }}
                  className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-900"
                >
                  Rename
                </button>
              </div>
            )}
            <p className="mt-2 text-sm text-zinc-500">
              {project.category}
              {exportFormatLabel(selectedExportFormat(project) || null) ? ` · ${exportFormatLabel(selectedExportFormat(project) || null)}` : ""}
              {` · ${project.total_duration}s · ${project.scenes.length} scenes`}
            </p>
          </div>
          <StatusPill project={project} />
        </header>

        {project.category === LUXURY_CATEGORY && (
          <LuxuryStudio project={project} busy={pending} run={(key, fn) => { void act(key, fn); }} />
        )}

        {error && (
          <p className="mb-6 rounded-xl border border-red-900 bg-red-950/50 p-4 text-sm text-red-300">{error}</p>
        )}
        {project.status === "failed" && project.error && (
          <p className="mb-6 rounded-xl border border-red-900 bg-red-950/50 p-4 text-sm text-red-300">
            <strong className="font-semibold">Render failed.</strong> {project.error}
            {videoUrl ? " Your previous reel is still available below." : " You can try again."}
          </p>
        )}

        <section className="mb-8 rounded-2xl border border-zinc-800 bg-zinc-900 p-4 sm:p-6">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="font-semibold">Photos</h2>
              <p className="mt-1 text-sm text-zinc-500">
                {project.category === LUXURY_CATEGORY
                  ? "The five scenes stay in place. New photos fill empty scenes, then stay in the library."
                  : images.length ? `${images.length} uploaded. Order follows the scenes.` : "No photos yet."}
              </p>
            </div>
            <label className="flex cursor-pointer items-center gap-2 rounded-xl border border-dashed border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-950">
              {pending?.startsWith("Uploading") ? <Loader2 size={16} className="animate-spin"/> : <ImagePlus size={16}/>}
              {pending?.startsWith("Uploading") ? pending : "Add photos"}
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp,image/bmp"
                multiple
                aria-label="Add photos"
                className="hidden"
                onChange={e => {
                  const list = Array.from(e.target.files ?? []);
                  e.target.value = "";
                  void addPhotos(list);
                }}
              />
            </label>
          </div>
          {images.length === 0 ? (
            <p className="mt-4 rounded-xl border border-dashed border-zinc-800 p-6 text-center text-sm text-zinc-500">
              {project.category === LUXURY_CATEGORY
                ? "Add photos for the five scenes, or choose images already in this project."
                : "Add one or more photos. Each photo becomes its own scene."}
            </p>
          ) : (
            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              {project.scenes.map((scene, index) => {
                const asset = images.find(item => item.id === scene.asset_id);
                const thumb = mediaUrl(scene.asset_url);
                const selected = selectedAsset === (scene.asset_id ?? scene.id);
                return (
                  <div
                    key={scene.id}
                    className={`rounded-xl border p-2 ${selected ? "border-violet-500" : "border-zinc-800"}`}
                  >
                    <button
                      type="button"
                      onClick={() => setSelectedAsset(scene.asset_id ?? scene.id)}
                      className="block w-full overflow-hidden rounded-lg bg-zinc-950 text-left"
                    >
                      {thumb
                        ? <img src={thumb} alt="" className="aspect-[4/3] w-full object-cover" />
                        : <span className="flex aspect-[4/3] items-center justify-center text-sm text-zinc-500">No photo</span>}
                    </button>
                    <p className="mt-2 truncate text-sm font-medium">{scene.text_title || scene.title}</p>
                    <p className="truncate text-xs text-zinc-500">
                      Scene {index + 1} · {scene.duration}s{asset ? ` · ${asset.filename}` : ""}
                    </p>
                    <div className="mt-2 flex flex-wrap gap-1">
                      <button type="button" aria-label={`Move scene ${index + 1} earlier`} disabled={index === 0 || pending === "order"} onClick={() => moveScene(index, -1)} className="rounded border border-zinc-700 p-1 disabled:opacity-30"><ChevronUp size={14}/></button>
                      <button type="button" aria-label={`Move scene ${index + 1} later`} disabled={index === project.scenes.length - 1 || pending === "order"} onClick={() => moveScene(index, 1)} className="rounded border border-zinc-700 p-1 disabled:opacity-30"><ChevronDown size={14}/></button>
                      {scene.asset_id && (
                        <button
                          type="button"
                          aria-label={`Remove photo from scene ${index + 1}`}
                          className="rounded border border-zinc-700 p-1 text-zinc-400 hover:text-red-300"
                          onClick={() => {
                            if (!window.confirm("Remove this photo from the scene? The scene itself stays.")) return;
                            void act(`del-asset-${scene.id}`, () => api.removeScenePhoto(reel.id, scene.id));
                          }}
                        >
                          <Trash2 size={14}/>
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <div className="grid gap-8 lg:grid-cols-[1fr_340px]">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-semibold">Scenes · {project.total_duration}s total</h2>
              <button
                type="button"
                disabled={project.status === "rendering" || pending === "add-scene"}
                onClick={() => act("add-scene", () => api.addScene(reel.id))}
                className="rounded-lg border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-900 disabled:opacity-50"
              >
                Add scene
              </button>
            </div>
            {project.scenes.map((scene, i) => (
              <SceneCard
                key={scene.id}
                scene={scene}
                index={i}
                pending={pending}
                onRegenerate={() => act(`regen-${scene.id}`, () => api.regenerateScene(reel.id, scene.id))}
                onSave={patch => act(`save-${scene.id}`, () => api.updateScene(reel.id, scene.id, patch))}
                onUpload={file => act(`up-${scene.id}`, () => api.upload(reel.id, file, scene.id, { replan: false }))}
                onAssign={assetId => act(`img-${scene.id}`, () => api.assignSceneAsset(reel.id, scene.id, assetId))}
                images={images}
                onRetry={() => act(`retry-${scene.id}`, () => api.retryScene(reel.id, scene.id))}
                canRetry={project.status !== "rendering"}
                onPreview={() => setPreviewScene(scene)}
                onMoveUp={() => moveScene(i, -1)}
                onMoveDown={() => moveScene(i, 1)}
                isFirst={i === 0}
                isLast={i === project.scenes.length - 1}
                onRemove={() => {
                  if (!window.confirm(`Remove “${scene.title}”? Its photo stays in the project until you delete it.`)) return;
                  void act(`del-scene-${scene.id}`, () => api.deleteScene(reel.id, scene.id));
                }}
              />
            ))}
          </div>

          <aside className="h-fit space-y-4">
            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-4 sm:p-6">
              <h2 className="font-semibold">Export</h2>
              {project.output_stale && videoUrl && (
                <p className="mt-3 rounded-lg border border-amber-900 bg-amber-950/40 p-3 text-sm text-amber-200">
                  This reel is out of date. Render again to include your latest changes. You can still play or download the previous version.
                </p>
              )}
              <div className={`mt-4 overflow-hidden rounded-xl bg-zinc-950 ${RATIO_CLASS[project.aspect_ratio] ?? "aspect-[9/16]"}`}>
                {videoUrl ? (
                  <video key={videoUrl} src={videoUrl} controls playsInline className="h-full w-full" />
                ) : (
                  <div className="flex h-full items-center justify-center px-6 text-center text-sm text-zinc-600">
                    {project.status === "rendering" ? "Rendering…" : "Generate the reel to see it here."}
                  </div>
                )}
              </div>

              {project.status === "rendering" && (
                <div className="mt-4">
                  <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
                    <div className="h-full bg-violet-500 transition-all" style={{ width: `${project.progress}%` }} />
                  </div>
                  <p className="mt-2 text-xs text-zinc-500">{project.progress}%</p>
                </div>
              )}

              <div className="mt-5">
                <label htmlFor="publish-format" className="text-sm text-zinc-400">Publish format</label>
                <select
                  id="publish-format"
                  aria-label="Publish format"
                  value={selectedExportFormat(project)}
                  disabled={project.status === "rendering" || pending === "export"}
                  onChange={e => {
                    const next = e.target.value as ExportFormat;
                    if (next) void act("export", () => api.updateExport(reel.id, next));
                  }}
                  className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500 disabled:opacity-50"
                >
                  {selectedExportFormat(project) === "" && <option value="">Choose a format</option>}
                  {EXPORT_FORMATS.map(item => (
                    <option key={item.id} value={item.id}>{item.label}</option>
                  ))}
                </select>
                {format && (
                  <div className="mt-3 space-y-1 text-sm text-zinc-300">
                    <p className="font-medium text-white">{format.label}</p>
                    <p>{format.ratio} · {format.width} × {format.height} · {format.fps} fps</p>
                    <p>About {project.total_duration}s</p>
                    <p>{musicOn ? "Music on" : "No music"}</p>
                  </div>
                )}
              </div>

              {imageGap && (
                <p className="mt-5 text-sm text-amber-200">{imageGap}</p>
              )}

              <button
                onClick={() => act("render", () => api.render(reel.id))}
                disabled={project.status === "rendering" || pending === "render" || !format || Boolean(imageGap)}
                className="mt-5 flex w-full items-center justify-center gap-2 rounded-xl bg-violet-500 px-5 py-3.5 font-semibold hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {project.status === "rendering"
                  ? <><Loader2 size={18} className="animate-spin"/> Rendering</>
                  : <>{project.status === "failed" ? "Try again" : project.status === "ready" || project.output_stale ? "Render again" : "Render"} <WandSparkles size={18}/></>}
              </button>

              {reel.status === "rendering" && reel.job && (
                <button
                  onClick={() => act("cancel", () => api.cancelJob(reel.id, reel.job!.id))}
                  disabled={pending === "cancel"}
                  className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-zinc-800 px-5 py-3 text-sm font-semibold text-zinc-300 hover:bg-zinc-950 disabled:opacity-50"
                >
                  <X size={16}/> Cancel
                </button>
              )}

              {videoUrl && (
                <button
                  type="button"
                  onClick={() => void downloadReel()}
                  disabled={pending === "download"}
                  className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-zinc-800 px-5 py-3 text-sm font-semibold text-zinc-200 hover:bg-zinc-950 disabled:opacity-50"
                >
                  {pending === "download" ? <Loader2 size={16} className="animate-spin"/> : <Download size={16}/>}
                  Download MP4
                </button>
              )}

              {desktop && reel.status === "ready" && !reel.output_stale && exportPhase === "idle" && (
                <button
                  type="button"
                  onClick={() => void saveToDownloads()}
                  className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-zinc-800 px-5 py-3 text-sm font-semibold text-zinc-200 hover:bg-zinc-950"
                >
                  <Download size={16}/> Save to Downloads
                </button>
              )}
              {desktop && exportPhase === "saving" && (
                <p className="mt-3 flex items-center gap-2 text-sm text-zinc-300">
                  <Loader2 size={16} className="animate-spin"/> Saving export
                </p>
              )}
              {desktop && exportPhase === "saved" && exportName && (
                <div className="mt-3 space-y-2 text-sm text-emerald-200">
                  <p>Saved {exportName}</p>
                  <button
                    type="button"
                    onClick={() => void desktop.revealExport(exportName)}
                    className="flex items-center gap-2 text-zinc-200"
                  >
                    <FolderOpen size={16}/> Show in folder
                  </button>
                </div>
              )}
              {desktop && exportPhase === "failed" && (
                <div className="mt-3 space-y-2 text-sm text-amber-200">
                  <p>{exportNote || "The reel is ready here, but it could not be saved to Downloads."}</p>
                  <button type="button" onClick={() => void saveToDownloads()} className="font-semibold text-white">
                    Try saving again
                  </button>
                </div>
              )}

              <div className="mt-5 border-t border-zinc-800 pt-5">
                {mockPreview ? (
                  <p className="text-sm text-zinc-400">{MOCK_QUALITY_NOTE}</p>
                ) : (
                  <>
                    <div className="flex items-center justify-between gap-3 text-sm">
                      <label htmlFor="quality" className="text-zinc-400">Quality</label>
                      <select
                        id="quality"
                        value={project.quality}
                        disabled={project.status === "rendering" || pending === "quality"}
                        onChange={e => act("quality", () => api.updateQuality(reel.id, e.target.value as Quality))}
                        className="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500 disabled:opacity-50"
                      >
                        {QUALITIES.map(q => (
                          <option key={q.value} value={q.value}>{q.label}</option>
                        ))}
                      </select>
                    </div>
                    <p className="mt-2 text-xs text-zinc-600">Changing quality regenerates every scene.</p>
                  </>
                )}
              </div>
            </section>

            <AudioStudio
              music={music}
              enabled={project.music_enabled}
              volume={project.music_volume}
              fadeIn={project.music_fade_in}
              fadeOut={project.music_fade_out}
              busy={pending === "music" || pending === "audio"}
              uploading={pending === "music"}
              onUpload={file => act("music", () => api.upload(reel.id, file))}
              onCommit={settings => act("audio", () => api.updateAudio(reel.id, settings))}
            />

            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
              <h2 className="font-semibold">The idea</h2>
              <p className="mt-2 text-sm leading-6 text-zinc-400">{reel.idea}</p>
            </section>
          </aside>
        </div>
      </div>
      {previewScene && (
        <div className="fixed inset-0 z-20 flex items-end justify-center bg-black/70 p-4 sm:items-center" role="dialog" aria-label="Scene preview">
          <div className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900 p-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="font-semibold">{previewScene.text_title || previewScene.title}</h2>
              <button type="button" onClick={() => setPreviewScene(null)} aria-label="Close preview" className="rounded-lg border border-zinc-700 p-2"><X size={16}/></button>
            </div>
            <div className="relative mt-3 overflow-hidden rounded-xl bg-zinc-950">
              {mediaUrl(previewScene.asset_url)
                ? <img src={mediaUrl(previewScene.asset_url)!} alt="" className="max-h-[60vh] w-full object-contain" />
                : <p className="p-8 text-center text-sm text-zinc-500">This scene has no photo yet.</p>}
              {(previewScene.show_title || previewScene.show_subtitle) && (
                <div className="absolute inset-x-0 bottom-[10%] px-4 text-center">
                  {previewScene.show_title && previewScene.text_title && (
                    <p className="text-lg text-white" style={{ fontFamily: "Georgia, serif", textShadow: "0 1px 2px rgba(0,0,0,0.85)" }}>{previewScene.text_title}</p>
                  )}
                  {previewScene.show_subtitle && previewScene.text_subtitle && (
                    <p className="mt-1 text-sm text-white/80" style={{ textShadow: "0 1px 2px rgba(0,0,0,0.85)" }}>{previewScene.text_subtitle}</p>
                  )}
                </div>
              )}
            </div>
            <p className="mt-3 text-sm text-zinc-400">{previewScene.duration}s</p>
          </div>
        </div>
      )}
    </main>
  );
}

const FADE_CHOICES = [0, 1, 2, 3, 5];

function AudioStudio({
  music, enabled, volume, fadeIn, fadeOut, busy, uploading, onUpload, onCommit,
}: {
  music: Asset | undefined;
  enabled: boolean;
  volume: number;
  fadeIn: number;
  fadeOut: number;
  busy: boolean;
  uploading: boolean;
  onUpload: (file: File) => void;
  onCommit: (settings: {
    music_volume?: number;
    music_fade_in?: number;
    music_fade_out?: number;
    music_enabled?: boolean;
  }) => void;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [draft, setDraft] = useState(volume);
  const src = music ? mediaUrl(music.url) : null;
  useEffect(() => setDraft(volume), [volume]);

  useEffect(() => {
    const el = audioRef.current;
    setPlaying(false);
    return () => {
      if (!el) return;
      el.pause();
      el.removeAttribute("src");
      el.load();
    };
  }, [src]);

  function togglePreview() {
    const el = audioRef.current;
    if (!el) return;
    if (el.paused) {
      void el.play().then(() => setPlaying(true)).catch(() => setPlaying(false));
    } else {
      el.pause();
      setPlaying(false);
    }
  }

  return (
    <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
      <h2 className="flex items-center gap-2 font-semibold"><Music size={16}/> Audio</h2>
      <p className="mt-1 text-sm text-zinc-500">
        {music ? music.filename : "No music yet. The reel stays silent until you add a track."}
      </p>
      <label className="mt-4 flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-700 p-4 text-sm text-zinc-400 hover:bg-zinc-950">
        {uploading ? <Loader2 size={16} className="animate-spin"/> : <Music size={16}/>}
        {music ? "Replace track" : "Add a track"}
        <input
          type="file"
          accept="audio/mpeg,audio/mp4,audio/aac,audio/wav,.mp3,.m4a,.aac,.wav"
          className="hidden"
          onChange={e => {
            const file = e.target.files?.[0];
            if (file) onUpload(file);
            e.target.value = "";
          }}
        />
      </label>

      {music && src && (
        <div className="mt-5 space-y-4 border-t border-zinc-800 pt-5">
          <audio
            ref={audioRef}
            src={src}
            preload="none"
            onEnded={() => setPlaying(false)}
          />
          <button
            type="button"
            onClick={togglePreview}
            className="flex items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-950"
          >
            {playing ? <Pause size={14}/> : <Play size={14}/>}
            {playing ? "Pause" : "Play"}
          </button>
          <div className="flex items-center justify-between gap-3 text-sm">
            <label htmlFor="music-enabled" className="text-zinc-400">Use this track</label>
            <button
              id="music-enabled"
              type="button"
              role="switch"
              aria-checked={enabled}
              disabled={busy}
              onClick={() => onCommit({ music_enabled: !enabled })}
              className={`relative h-6 w-11 rounded-full transition-colors disabled:opacity-50 ${enabled ? "bg-violet-500" : "bg-zinc-700"}`}
            >
              <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-transform ${enabled ? "left-5" : "left-0.5"}`} />
            </button>
          </div>
          <div>
            <div className="flex items-center justify-between text-sm">
              <label htmlFor="music-volume" className="text-zinc-400">Volume</label>
              <span className="text-zinc-300">{Math.round(draft * 100)}%</span>
            </div>
            <input
              id="music-volume"
              type="range"
              min={0}
              max={1.5}
              step={0.05}
              value={draft}
              disabled={busy}
              onChange={e => setDraft(Number(e.target.value))}
              onPointerUp={() => draft !== volume && onCommit({ music_volume: draft })}
              onKeyUp={() => draft !== volume && onCommit({ music_volume: draft })}
              className="mt-2 w-full accent-violet-500"
            />
          </div>
          <FadeSelect
            id="music-fade-in"
            label="Fade in"
            value={fadeIn}
            disabled={busy}
            onChange={seconds => onCommit({ music_fade_in: seconds })}
          />
          <FadeSelect
            id="music-fade-out"
            label="Fade out"
            value={fadeOut}
            disabled={busy}
            onChange={seconds => onCommit({ music_fade_out: seconds })}
          />
          <p className="text-xs text-zinc-600">Render again to hear this in the reel.</p>
        </div>
      )}
    </section>
  );
}

function FadeSelect({
  id, label, value, disabled, onChange,
}: {
  id: string;
  label: string;
  value: number;
  disabled: boolean;
  onChange: (seconds: number) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <label htmlFor={id} className="text-zinc-400">{label}</label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={e => onChange(Number(e.target.value))}
        className="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500"
      >
        {FADE_CHOICES.map(seconds => (
          <option key={seconds} value={seconds}>{seconds === 0 ? "None" : `${seconds}s`}</option>
        ))}
      </select>
    </div>
  );
}

function StatusPill({ project }: { project: Project }) {
  const styles: Record<string, string> = {
    draft: "border-zinc-700 text-zinc-400",
    rendering: "border-violet-600 bg-violet-500/10 text-violet-300",
    ready: "border-emerald-700 bg-emerald-500/10 text-emerald-300",
    failed: "border-red-800 bg-red-500/10 text-red-300",
  };
  const label = {
    draft: project.output_stale && project.video_url ? "Out of date" : "Draft",
    rendering: `Rendering ${project.progress}%`,
    ready: "Ready",
    failed: "Failed",
  };
  return (
    <span className={`rounded-full border px-3 py-1.5 text-xs font-medium ${styles[project.status]}`}>
      {label[project.status]}
    </span>
  );
}

function SceneText({
  scene, pending, onSave,
}: {
  scene: Scene;
  pending: string | null;
  onSave: (patch: Partial<Pick<Scene, "text_title" | "text_subtitle" | "show_title" | "show_subtitle">>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [showTitle, setShowTitle] = useState(scene.show_title);
  const [showSubtitle, setShowSubtitle] = useState(scene.show_subtitle);
  const [textTitle, setTextTitle] = useState(scene.text_title ?? "");
  const [textSubtitle, setTextSubtitle] = useState(scene.text_subtitle ?? "");

  useEffect(() => {
    setShowTitle(scene.show_title);
    setShowSubtitle(scene.show_subtitle);
    setTextTitle(scene.text_title ?? "");
    setTextSubtitle(scene.text_subtitle ?? "");
  }, [scene.show_title, scene.show_subtitle, scene.text_title, scene.text_subtitle]);

  const thumb = mediaUrl(scene.asset_url);
  const busy = pending === `save-${scene.id}`;

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        className="text-xs font-medium text-zinc-400 hover:text-white"
      >
        Text{scene.show_title && scene.text_title ? ` · ${scene.text_title}` : ""}
      </button>
      {open && (
        <div className="mt-3 grid gap-3 sm:grid-cols-[140px_1fr]">
          <div className="relative aspect-[9/16] overflow-hidden rounded-xl bg-zinc-800">
            {thumb
              ? <img src={thumb} alt="" className="h-full w-full object-cover" />
              : <div className="h-full w-full bg-zinc-800" />}
            <div className="absolute inset-x-2 bottom-[18%] text-center">
              {showTitle && textTitle.trim() && (
                <p className="text-[11px] font-light leading-tight text-white" style={{ textShadow: "0 1px 2px rgba(0,0,0,0.75)" }}>
                  {textTitle}
                </p>
              )}
              {showSubtitle && textSubtitle.trim() && (
                <p className="mt-1 text-[9px] font-light leading-tight text-white/90" style={{ textShadow: "0 1px 2px rgba(0,0,0,0.75)" }}>
                  {textSubtitle}
                </p>
              )}
            </div>
          </div>
          <div className="space-y-2">
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <input type="checkbox" checked={showTitle} onChange={e => setShowTitle(e.target.checked)} />
              Title
            </label>
            <input
              value={textTitle}
              onChange={e => setTextTitle(e.target.value)}
              maxLength={80}
              aria-label="Scene title text"
              placeholder="Title"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500"
            />
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <input type="checkbox" checked={showSubtitle} onChange={e => setShowSubtitle(e.target.checked)} />
              Subtitle
            </label>
            <input
              value={textSubtitle}
              onChange={e => setTextSubtitle(e.target.value)}
              maxLength={120}
              aria-label="Scene subtitle"
              placeholder="Subtitle (optional)"
              className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => onSave({
                text_title: textTitle.trim(),
                text_subtitle: textSubtitle.trim(),
                show_title: showTitle,
                show_subtitle: showSubtitle,
              })}
              className="rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-semibold hover:bg-violet-400 disabled:opacity-50"
            >
              {busy ? "Saving" : "Save text"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SceneCard({
  scene, index, pending, images, onRegenerate, onSave, onUpload, onAssign, onRetry, canRetry,
  onPreview, onMoveUp, onMoveDown, onRemove, isFirst, isLast,
}: {
  scene: Scene;
  index: number;
  pending: string | null;
  images: Asset[];
  onRegenerate: () => void;
  onSave: (patch: Partial<Pick<Scene, "title" | "prompt" | "caption" | "duration" | "text_title" | "text_subtitle" | "show_title" | "show_subtitle">>) => void;
  onUpload: (file: File) => void;
  onAssign: (assetId: string) => void;
  onRetry: () => void;
  canRetry: boolean;
  onPreview: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
  isFirst: boolean;
  isLast: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [durationError, setDurationError] = useState<string | null>(null);
  const [title, setTitle] = useState(scene.title);
  const [prompt, setPrompt] = useState(scene.prompt);
  const [caption, setCaption] = useState(scene.caption ?? "");
  const [duration, setDuration] = useState(scene.duration);

  // The API returns the whole project after every mutation, so reset the draft
  // fields whenever the server's version of this scene changes.
  useEffect(() => {
    setTitle(scene.title);
    setPrompt(scene.prompt);
    setCaption(scene.caption ?? "");
    setDuration(scene.duration);
  }, [scene.title, scene.prompt, scene.caption, scene.duration]);

  const thumb = mediaUrl(scene.asset_url);
  const busy = pending === `regen-${scene.id}` || pending === `save-${scene.id}` || pending === `up-${scene.id}` || pending === `img-${scene.id}`;

  return (
    <div className="grid gap-4 rounded-2xl border border-zinc-800 bg-zinc-900 p-5 md:grid-cols-[90px_1fr_auto] md:items-start">
      <div
        title="Replace this scene's still"
        className="group relative flex h-20 w-full cursor-pointer items-center justify-center overflow-hidden rounded-xl bg-zinc-800 text-2xl"
      >
        {thumb
          ? <img src={thumb} alt="" className="h-full w-full object-cover" />
          : <span className="text-zinc-400">{index + 1}</span>}
        <span className="absolute inset-0 hidden items-center justify-center bg-black/60 group-hover:flex">
          <ImagePlus size={18} />
        </span>
        <input
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp"
          aria-label="Replace this scene's still"
          className="absolute inset-0 cursor-pointer opacity-0"
          onChange={e => {
            const f = e.target.files?.[0];
            if (f) onUpload(f);
            e.target.value = "";
          }}
        />
      </div>

      {editing ? (
        <div className="space-y-3">
          <input value={title} onChange={e => setTitle(e.target.value)} aria-label="Scene title" className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm font-semibold outline-none focus:border-violet-500" />
          <textarea value={prompt} onChange={e => setPrompt(e.target.value)} aria-label="Scene prompt" rows={3} className="w-full resize-none rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500" />
          <input value={caption} onChange={e => setCaption(e.target.value)} placeholder="On-screen caption (optional)" aria-label="Caption" className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500" />
          <div className="flex items-center gap-3">
            <label className="text-xs text-zinc-500" htmlFor={`dur-${scene.id}`}>Seconds</label>
            <input id={`dur-${scene.id}`} type="number" min={1} max={30} value={duration} onChange={e => { setDuration(Number(e.target.value)); setDurationError(null); }} className="w-20 rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500" />
          </div>
          {durationError && <p className="text-xs text-red-300">{durationError}</p>}
        </div>
      ) : (
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="font-semibold">{scene.title}</h2>
            <span className="rounded-full bg-zinc-800 px-2 py-1 text-xs text-zinc-400">{scene.duration}s</span>
            {scene.regen_count > 0 && (
              <span
                title={`Regenerated ${scene.regen_count} time${scene.regen_count === 1 ? "" : "s"}`}
                className="rounded-full bg-zinc-800 px-2 py-1 text-xs text-zinc-400"
              >
                v{scene.regen_count + 1}
              </span>
            )}
            {scene.clip_status === "ready" && (
              <span title="This scene has been generated" className="rounded-full bg-emerald-500/15 px-2 py-1 text-xs text-emerald-300">Generated</span>
            )}
            {scene.clip_status === "failed" && (
              <span className="rounded-full bg-amber-500/15 px-2 py-1 text-xs text-amber-300">Needs another try</span>
            )}
            {scene.caption && <span className="rounded-full bg-violet-500/15 px-2 py-1 text-xs text-violet-300">“{scene.caption}”</span>}
          </div>
          {scene.clip_status === "failed" && (
            <p className="mt-2 text-sm text-amber-300/90">
              {/* Backend messages are already customer-safe: no keys, URLs or stack traces. */}
              {scene.clip_error || "This scene couldn’t be generated."}
            </p>
          )}
          <p className="mt-2 text-sm leading-6 text-zinc-400">{scene.prompt}</p>
          <label className="mt-3 flex flex-wrap items-center gap-2 text-xs text-zinc-500" htmlFor={`img-${scene.id}`}>
            Change image
            <select
              id={`img-${scene.id}`}
              aria-label={`Change image for ${scene.title}`}
              value={scene.asset_id ?? ""}
              disabled={busy || images.length === 0}
              onChange={e => {
                const next = e.target.value;
                if (next && next !== scene.asset_id) onAssign(next);
              }}
              className="rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-200 outline-none focus:border-violet-500 disabled:opacity-50"
            >
              {images.length === 0 && <option value="">No photos yet</option>}
              {images.length > 0 && !scene.asset_id && <option value="">Choose a photo</option>}
              {images.map((asset, imageIndex) => (
                <option key={asset.id} value={asset.id}>
                  {asset.filename || `Photo ${imageIndex + 1}`}
                </option>
              ))}
            </select>
          </label>
          <SceneText scene={scene} pending={pending} onSave={onSave} />
        </div>
      )}

      <div className="flex gap-2 md:flex-col">
        {scene.clip_status === "failed" && !editing ? (
          <button
            onClick={onRetry}
            disabled={!canRetry || pending === `retry-${scene.id}`}
            className="flex items-center gap-2 rounded-lg bg-amber-500/15 px-4 py-2 text-sm font-semibold text-amber-300 hover:bg-amber-500/25 disabled:opacity-50"
          >
            {pending === `retry-${scene.id}`
              ? <Loader2 size={15} className="animate-spin"/>
              : <RotateCcw size={15}/>} Try again
          </button>
        ) : null}
        {editing ? (
          <>
            <button
              onClick={() => {
                if (!Number.isInteger(duration) || duration < 1 || duration > 30) {
                  setDurationError("Enter a scene length from 1 to 30 seconds.");
                  return;
                }
                setDurationError(null);
                onSave({ title, prompt, caption: caption.trim() || undefined, duration });
                setEditing(false);
              }}
              className="flex items-center gap-2 rounded-lg bg-violet-500 px-4 py-2 text-sm font-semibold hover:bg-violet-400"
            >
              <Check size={15}/> Save
            </button>
            <button onClick={() => setEditing(false)} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800">Cancel</button>
          </>
        ) : (
          <>
            <button onClick={() => setEditing(true)} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800">Edit scene</button>
            <button type="button" onClick={onPreview} className="flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800"><Eye size={15}/> Preview</button>
            <button type="button" onClick={onMoveUp} disabled={isFirst || pending === "order"} aria-label="Move scene earlier" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800 disabled:opacity-30"><ChevronUp size={15}/></button>
            <button type="button" onClick={onMoveDown} disabled={isLast || pending === "order"} aria-label="Move scene later" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800 disabled:opacity-30"><ChevronDown size={15}/></button>
            <button type="button" onClick={onRemove} className="flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800"><Trash2 size={15}/> Remove</button>
            <button onClick={onRegenerate} disabled={busy} className="flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800 disabled:opacity-50">
              <RefreshCw size={15} className={pending === `regen-${scene.id}` ? "animate-spin" : ""}/> Regenerate
            </button>
          </>
        )}
      </div>
    </div>
  );
}
