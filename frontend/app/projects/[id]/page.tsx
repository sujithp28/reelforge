"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  AlertTriangle, ArrowLeft, Check, Download, ImagePlus, Loader2, Music,
  RefreshCw, WandSparkles,
} from "lucide-react";
import { api, mediaUrl, type Project, type Scene } from "../../../lib/api";

const RATIO_CLASS: Record<string, string> = {
  "9:16": "aspect-[9/16]",
  "16:9": "aspect-video",
  "1:1": "aspect-square",
  "4:5": "aspect-[4/5]",
};

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

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

  // Poll only while a render is actually in flight.
  useEffect(() => {
    if (project?.status !== "rendering") return;
    const timer = setInterval(() => void load(), 1500);
    return () => clearInterval(timer);
  }, [project?.status, load]);

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

  const videoUrl = mediaUrl(project.video_url);
  const music = project.assets.find(a => a.kind === "audio");

  return (
    <main className="min-h-screen bg-zinc-950 px-6 py-8">
      <div className="mx-auto max-w-6xl">
        <Link href="/dashboard" className="mb-8 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"><ArrowLeft size={16}/> Dashboard</Link>

        <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <p className="text-sm text-violet-400">Storyboard</p>
            <h1 className="mt-1 text-3xl font-semibold">{project.title}</h1>
            <p className="mt-2 text-sm text-zinc-500">
              {project.category} · {project.aspect_ratio} · {project.total_duration}s · {project.scenes.length} scenes
            </p>
          </div>
          <StatusPill project={project} />
        </header>

        {error && (
          <p className="mb-6 rounded-xl border border-red-900 bg-red-950/50 p-4 text-sm text-red-300">{error}</p>
        )}
        {project.status === "failed" && project.error && (
          <p className="mb-6 rounded-xl border border-red-900 bg-red-950/50 p-4 text-sm text-red-300">
            <strong className="font-semibold">Render failed.</strong> {project.error}
          </p>
        )}

        <div className="grid gap-8 lg:grid-cols-[1fr_340px]">
          <div className="space-y-3">
            {project.scenes.map((scene, i) => (
              <SceneCard
                key={scene.id}
                scene={scene}
                index={i}
                pending={pending}
                onRegenerate={() => act(`regen-${scene.id}`, () => api.regenerateScene(project.id, scene.id))}
                onSave={patch => act(`save-${scene.id}`, () => api.updateScene(project.id, scene.id, patch))}
                onUpload={file => act(`up-${scene.id}`, () => api.upload(project.id, file, scene.id))}
              />
            ))}
          </div>

          <aside className="h-fit space-y-4">
            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
              <h2 className="font-semibold">Preview</h2>
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

              <button
                onClick={() => act("render", () => api.render(project.id))}
                disabled={project.status === "rendering" || pending === "render"}
                className="mt-5 flex w-full items-center justify-center gap-2 rounded-xl bg-violet-500 px-5 py-3.5 font-semibold hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {project.status === "rendering"
                  ? <><Loader2 size={18} className="animate-spin"/> Rendering</>
                  : <>{project.status === "ready" ? "Re-generate reel" : "Generate reel"} <WandSparkles size={18}/></>}
              </button>

              {videoUrl && (
                <a href={videoUrl} download={`${project.title.slice(0, 40)}.mp4`} className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-zinc-800 px-5 py-3 text-sm font-semibold text-zinc-200 hover:bg-zinc-950">
                  <Download size={16}/> Download MP4
                </a>
              )}
            </section>

            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
              <h2 className="flex items-center gap-2 font-semibold"><Music size={16}/> Soundtrack</h2>
              <p className="mt-1 text-sm text-zinc-500">{music ? music.filename : "No music yet. Optional."}</p>
              <label className="mt-4 flex cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-700 p-4 text-sm text-zinc-400 hover:bg-zinc-950">
                {pending === "music" ? <Loader2 size={16} className="animate-spin"/> : <Music size={16}/>}
                {music ? "Replace track" : "Add a track"}
                <input
                  type="file"
                  accept="audio/mpeg,audio/mp4,audio/aac,audio/wav,audio/ogg,.mp3,.m4a,.wav,.ogg"
                  className="hidden"
                  onChange={e => {
                    const f = e.target.files?.[0];
                    if (f) void act("music", () => api.upload(project.id, f));
                    e.target.value = "";
                  }}
                />
              </label>

              {music && (
                <AudioSettings
                  volume={project.music_volume}
                  fadeOut={project.music_fade_out}
                  busy={pending === "audio"}
                  onCommit={settings => act("audio", () => api.updateAudio(project.id, settings))}
                />
              )}
            </section>

            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
              <h2 className="font-semibold">The idea</h2>
              <p className="mt-2 text-sm leading-6 text-zinc-400">{project.idea}</p>
            </section>
          </aside>
        </div>
      </div>
    </main>
  );
}

const FADE_CHOICES = [0, 1, 2, 3, 5];

function AudioSettings({
  volume, fadeOut, busy, onCommit,
}: {
  volume: number;
  fadeOut: number;
  busy: boolean;
  onCommit: (settings: { music_volume?: number; music_fade_out?: number }) => void;
}) {
  // Track the slider locally and only send on release: onChange fires per pixel,
  // and each request re-renders the reel's status.
  const [draft, setDraft] = useState(volume);
  useEffect(() => setDraft(volume), [volume]);

  return (
    <div className="mt-5 space-y-4 border-t border-zinc-800 pt-5">
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
      <div className="flex items-center justify-between gap-3 text-sm">
        <label htmlFor="music-fade" className="text-zinc-400">Fade out</label>
        <select
          id="music-fade"
          value={fadeOut}
          disabled={busy}
          onChange={e => onCommit({ music_fade_out: Number(e.target.value) })}
          className="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500"
        >
          {FADE_CHOICES.map(s => (
            <option key={s} value={s}>{s === 0 ? "None" : `${s}s`}</option>
          ))}
        </select>
      </div>
      <p className="text-xs text-zinc-600">Changing the mix re-renders the reel.</p>
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
  const label = { draft: "Draft", rendering: `Rendering ${project.progress}%`, ready: "Ready", failed: "Failed" };
  return (
    <span className={`rounded-full border px-3 py-1.5 text-xs font-medium ${styles[project.status]}`}>
      {label[project.status]}
    </span>
  );
}

function SceneCard({
  scene, index, pending, onRegenerate, onSave, onUpload,
}: {
  scene: Scene;
  index: number;
  pending: string | null;
  onRegenerate: () => void;
  onSave: (patch: Partial<Pick<Scene, "title" | "prompt" | "caption" | "duration">>) => void;
  onUpload: (file: File) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(scene.title);
  const [prompt, setPrompt] = useState(scene.prompt);
  const [caption, setCaption] = useState(scene.caption ?? "");
  const [duration, setDuration] = useState(scene.duration);
  const fileRef = useRef<HTMLInputElement>(null);

  // The API returns the whole project after every mutation, so reset the draft
  // fields whenever the server's version of this scene changes.
  useEffect(() => {
    setTitle(scene.title);
    setPrompt(scene.prompt);
    setCaption(scene.caption ?? "");
    setDuration(scene.duration);
  }, [scene.title, scene.prompt, scene.caption, scene.duration]);

  const thumb = mediaUrl(scene.asset_url);
  const busy = pending === `regen-${scene.id}` || pending === `save-${scene.id}` || pending === `up-${scene.id}`;

  return (
    <div className="grid gap-4 rounded-2xl border border-zinc-800 bg-zinc-900 p-5 md:grid-cols-[90px_1fr_auto] md:items-start">
      <button
        onClick={() => fileRef.current?.click()}
        title="Replace this scene's still"
        className="group relative flex h-20 w-full items-center justify-center overflow-hidden rounded-xl bg-zinc-800 text-2xl"
      >
        {thumb
          ? <img src={thumb} alt="" className="h-full w-full object-cover" />
          : <span className="text-zinc-400">{index + 1}</span>}
        <span className="absolute inset-0 hidden items-center justify-center bg-black/60 group-hover:flex">
          <ImagePlus size={18} />
        </span>
      </button>
      <input
        ref={fileRef}
        type="file"
        accept="image/jpeg,image/png,image/webp,image/bmp"
        className="hidden"
        onChange={e => {
          const f = e.target.files?.[0];
          if (f) onUpload(f);
          e.target.value = "";
        }}
      />

      {editing ? (
        <div className="space-y-3">
          <input value={title} onChange={e => setTitle(e.target.value)} aria-label="Scene title" className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm font-semibold outline-none focus:border-violet-500" />
          <textarea value={prompt} onChange={e => setPrompt(e.target.value)} aria-label="Scene prompt" rows={3} className="w-full resize-none rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500" />
          <input value={caption} onChange={e => setCaption(e.target.value)} placeholder="On-screen caption (optional)" aria-label="Caption" className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500" />
          <div className="flex items-center gap-3">
            <label className="text-xs text-zinc-500" htmlFor={`dur-${scene.id}`}>Seconds</label>
            <input id={`dur-${scene.id}`} type="number" min={1} max={30} value={duration} onChange={e => setDuration(Number(e.target.value))} className="w-20 rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-violet-500" />
          </div>
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
            {scene.caption && <span className="rounded-full bg-violet-500/15 px-2 py-1 text-xs text-violet-300">“{scene.caption}”</span>}
          </div>
          <p className="mt-2 text-sm leading-6 text-zinc-400">{scene.prompt}</p>
        </div>
      )}

      <div className="flex gap-2 md:flex-col">
        {editing ? (
          <>
            <button
              onClick={() => { onSave({ title, prompt, caption: caption.trim() || undefined, duration }); setEditing(false); }}
              className="flex items-center gap-2 rounded-lg bg-violet-500 px-4 py-2 text-sm font-semibold hover:bg-violet-400"
            >
              <Check size={15}/> Save
            </button>
            <button onClick={() => setEditing(false)} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800">Cancel</button>
          </>
        ) : (
          <>
            <button onClick={() => setEditing(true)} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800">Edit scene</button>
            <button onClick={onRegenerate} disabled={busy} className="flex items-center gap-2 rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800 disabled:opacity-50">
              <RefreshCw size={15} className={pending === `regen-${scene.id}` ? "animate-spin" : ""}/> Regenerate
            </button>
          </>
        )}
      </div>
    </div>
  );
}
