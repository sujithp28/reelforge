"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, ImagePlus, Loader2, Sparkles, Upload, X } from "lucide-react";
import {
  api, CATEGORIES, DURATIONS, INPUT_TYPES, QUALITIES, RATIOS,
  type Project, type Quality, type Ratio,
} from "../../lib/api";

const MAX_REFERENCE_IMAGES = 8;

/** One photo fills every scene. Several photos go in scene order, and the last photo covers any scenes left over. */
async function attachReferences(projectId: string, scenes: Project["scenes"], chosen: File[]) {
  if (chosen.length === 1) {
    const saved = await api.upload(projectId, chosen[0]);
    if (saved.scenes.some(scene => !scene.asset_url)) {
      throw new Error("The photo uploaded, but it was not attached to every scene.");
    }
    return;
  }

  const ordered = [...scenes].sort((a, b) => a.position - b.position);
  const count = Math.min(chosen.length, ordered.length);
  let current: Project | null = null;
  for (let i = 0; i < count; i++) {
    current = await api.upload(projectId, chosen[i], ordered[i].id);
  }
  if (!current) throw new Error("No photos were attached.");
  const last = current.scenes.find(scene => scene.id === ordered[count - 1].id);
  if (!last?.asset_id) throw new Error("The last photo was not attached to its scene.");
  for (let i = count; i < ordered.length; i++) {
    current = await api.assignSceneAsset(projectId, ordered[i].id, last.asset_id);
  }
  // Photos beyond the scene count stay on the project so a scene can switch to them.
  // Every scene already has an image, so these uploads do not fill bare scenes.
  for (let i = count; i < chosen.length; i++) {
    current = await api.upload(projectId, chosen[i]);
  }
  if (current.scenes.some(scene => !scene.asset_url)) {
    throw new Error("A scene was left without a photo.");
  }
}

function CreateForm() {
  const router = useRouter();
  const params = useSearchParams();
  // The landing page links here with ?type=Product, so honour it.
  const initialType = params.get("type");

  const [type, setType] = useState(
    CATEGORIES.includes((initialType ?? "") as (typeof CATEGORIES)[number])
      ? (initialType as string)
      : "Cinematic",
  );
  const [idea, setIdea] = useState("");
  const [input, setInput] = useState<string>("Idea");
  const [ratio, setRatio] = useState<Ratio>("9:16");
  const [duration, setDuration] = useState(30);
  const [quality, setQuality] = useState<Quality>("standard");
  const [files, setFiles] = useState<File[]>([]);
  const [previews, setPreviews] = useState<string[]>([]);
  // The storyboard request is JSON and cannot carry the photos. generate()
  // reads this ref so a selection cannot be dropped by a stale render.
  const filesRef = useRef<File[]>([]);
  // The file input sits on top of the drop zone. It is not display:none and
  // not inside a label, so one click opens one dialog and onChange keeps the file.
  const pickerRef = useRef<HTMLInputElement | null>(null);
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set once create succeeds, so a failed reference upload can be retried
  // against the same project instead of silently building a title-card reel.
  const [createdId, setCreatedId] = useState<string | null>(null);

  const ready = useMemo(() => idea.trim().length > 0, [idea]);

  function setChosen(next: File[]) {
    const capped = next.slice(0, MAX_REFERENCE_IMAGES);
    filesRef.current = capped;
    setFiles(capped);
    // A chosen photo is the reel. Leaving the source on Idea let a missed
    // picker continue into title cards.
    if (capped.length > 0 && input === "Idea") setInput("Image");
  }

  function takeFiles(list: FileList | null) {
    if (!list?.length) return;
    const incoming = Array.from(list).filter(file =>
      file.type.startsWith("image/") || /\.(jpe?g|png|webp|bmp)$/i.test(file.name),
    );
    if (!incoming.length) return;
    setChosen([...filesRef.current, ...incoming]);
  }

  function removeAt(index: number) {
    setChosen(filesRef.current.filter((_, i) => i !== index));
  }

  useEffect(() => {
    const urls = files.map(file => URL.createObjectURL(file));
    setPreviews(urls);
    return () => {
      for (const url of urls) URL.revokeObjectURL(url);
    };
  }, [files]);

  // These sources mean the reel is the customer's picture. Idea can still
  // be title cards. Image cannot.
  const needsPhoto = input === "Image" || input === "Image + text" || input === "Product" || input === "Person";

  async function generate(allowWithoutPhoto = false) {
    setBusy(true);
    setError(null);
    const chosen = filesRef.current;
    if (chosen.length === 0 && !allowWithoutPhoto) {
      setError("Add at least one reference photo first.");
      setBusy(false);
      return;
    }
    try {
      let projectId = createdId;
      let scenes: Project["scenes"] | null = null;
      if (!projectId) {
        const project = await api.createProject({
          idea: idea.trim(),
          category: type,
          input_type: input,
          duration,
          aspect_ratio: ratio,
          quality,
        });
        projectId = project.id;
        scenes = project.scenes;
        setCreatedId(projectId);
      } else {
        scenes = (await api.getProject(projectId)).scenes;
      }
      // The create body is JSON, so the photos are the next requests. Every
      // scene has to point at an image before we leave this page.
      if (chosen.length > 0) {
        await attachReferences(projectId, scenes, chosen);
      }
      router.push(`/projects/${projectId}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the storyboard.");
      setBusy(false);
    }
  }

  if (step === 2) {
    return (
      <main className="min-h-screen bg-zinc-950 px-6 py-8">
        <div className="mx-auto max-w-5xl">
          <button onClick={() => setStep(1)} className="mb-8 flex items-center gap-2 text-sm text-zinc-400 hover:text-white"><ArrowLeft size={16}/> Back</button>
          <div className="mb-8">
            <p className="text-sm text-violet-400">Step 2 of 2</p>
            <h1 className="mt-2 text-3xl font-semibold">Your reel settings</h1>
            {files.length === 1 ? (
              <p className="mt-3 text-sm text-zinc-300">Reference photo: {files[0].name}. It will be used for every scene.</p>
            ) : files.length > 1 ? (
              <p className="mt-3 text-sm text-zinc-300">{files.length} reference photos. Each scene gets the next photo, and extra scenes reuse the last one.</p>
            ) : (
              <p className="mt-3 text-sm text-amber-200/90">No reference photo is selected, so the reel will be title cards. Go back to add one.</p>
            )}
          </div>
          <div className="grid gap-6 md:grid-cols-2">
            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
              <h2 className="font-semibold">Duration</h2>
              <p className="mt-1 text-sm text-zinc-500">Scene count and pacing follow the length you pick.</p>
              <div className="mt-4 grid grid-cols-4 gap-2">
                {DURATIONS.map(d => (
                  <button key={d} onClick={() => setDuration(d)} className={`rounded-xl border p-3 text-sm ${duration === d ? "border-violet-500 bg-violet-500/10 text-white" : "border-zinc-800 text-zinc-400 hover:border-zinc-700"}`}>{d}s</button>
                ))}
              </div>
              <label className="mt-5 block text-xs text-zinc-500" htmlFor="duration">Or set it exactly</label>
              <input
                id="duration"
                type="range"
                min={5}
                max={120}
                step={1}
                value={duration}
                onChange={e => setDuration(Number(e.target.value))}
                className="mt-2 w-full accent-violet-500"
              />
              <div className="mt-2 text-2xl font-semibold">{duration}s</div>
            </section>
            <section className="rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
              <h2 className="font-semibold">Aspect ratio</h2>
              <div className="mt-4 grid grid-cols-4 gap-2">
                {RATIOS.map(r => <button key={r} onClick={() => setRatio(r)} className={`rounded-xl border p-3 text-sm ${ratio === r ? "border-violet-500 bg-violet-500/10 text-white" : "border-zinc-800 text-zinc-400 hover:border-zinc-700"}`}>{r}</button>)}
              </div>
              <p className="mt-4 text-sm text-zinc-500">
                {ratio === "9:16" && "Vertical — Reels, Shorts, TikTok. 1080×1920."}
                {ratio === "16:9" && "Landscape — YouTube, web embeds. 1920×1080."}
                {ratio === "1:1" && "Square — feed posts. 1080×1080."}
                {ratio === "4:5" && "Portrait — Instagram feed. 1080×1350."}
              </p>
            </section>
          </div>

          <section className="mt-6 rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
            <h2 className="font-semibold">Quality</h2>
            <p className="mt-1 text-sm text-zinc-500">Applies when scenes are generated.</p>
            <div className="mt-4 grid gap-2 sm:grid-cols-2">
              {QUALITIES.map(q => (
                <button
                  key={q.value}
                  onClick={() => setQuality(q.value)}
                  className={`rounded-xl border p-4 text-left ${quality === q.value ? "border-violet-500 bg-violet-500/10 text-white" : "border-zinc-800 text-zinc-400 hover:border-zinc-700"}`}
                >
                  <div className="font-medium">{q.label}</div>
                  <div className="mt-1 text-xs text-zinc-500">{q.hint}</div>
                </button>
              ))}
            </div>
          </section>

          {error && (
            <p className="mt-6 rounded-xl border border-red-900 bg-red-950/50 p-4 text-sm text-red-300">{error}</p>
          )}

          <button onClick={() => generate(false)} disabled={busy || files.length === 0} className="mt-8 inline-flex items-center gap-2 rounded-xl bg-violet-500 px-6 py-3.5 font-semibold hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-50">
            {busy ? <><Loader2 size={18} className="animate-spin"/> Building storyboard</> : <>Generate storyboard <Sparkles size={18}/></>}
          </button>
          {files.length === 0 && (
            <button type="button" onClick={() => generate(true)} disabled={busy} className="mt-3 block text-sm text-zinc-500 underline-offset-2 hover:text-zinc-300 hover:underline">
              Generate title cards without a photo
            </button>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-zinc-950 px-6 py-8">
      <div className="mx-auto max-w-6xl">
        <Link href="/" className="mb-10 inline-flex items-center gap-2 text-sm text-zinc-400 hover:text-white"><ArrowLeft size={16}/> ReelForge</Link>
        <div className="mb-10">
          <p className="text-sm text-violet-400">Step 1 of 2</p>
          <h1 className="mt-2 text-4xl font-semibold">What are you creating?</h1>
          <p className="mt-2 text-zinc-500">Describe your idea. ReelForge will turn it into a storyboard.</p>
        </div>
        <div className="grid gap-8 lg:grid-cols-[1fr_380px]">
          <section>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {CATEGORIES.map(t => <button key={t} onClick={() => setType(t)} className={`rounded-2xl border p-4 text-left ${type === t ? "border-violet-500 bg-violet-500/10" : "border-zinc-800 bg-zinc-900 hover:border-zinc-700"}`}>{t}</button>)}
            </div>
            <div className="mt-6 rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
              <label className="text-sm font-medium" htmlFor="idea">Your idea</label>
              <textarea id="idea" value={idea} onChange={e => setIdea(e.target.value)} placeholder="Example: Create a 30-second luxury real-estate reel for this house..." className="mt-3 min-h-36 w-full resize-none rounded-xl border border-zinc-800 bg-zinc-950 p-4 text-sm outline-none placeholder:text-zinc-600 focus:border-violet-500" />
              <div className="mt-4 flex flex-wrap gap-2">
                {INPUT_TYPES.map(x => <button key={x} onClick={() => setInput(x)} className={`rounded-lg px-3 py-2 text-xs ${input === x ? "bg-white text-zinc-950" : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"}`}>{x}</button>)}
              </div>

              {files.length < MAX_REFERENCE_IMAGES && (
                <div
                  className="relative mt-4"
                  onDragOver={e => e.preventDefault()}
                  onDrop={e => {
                    e.preventDefault();
                    takeFiles(e.dataTransfer.files);
                  }}
                >
                  <div className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-700 p-7 text-sm text-zinc-400">
                    <Upload size={18}/> Upload reference photos <ImagePlus size={18}/>
                  </div>
                  <input
                    ref={pickerRef}
                    type="file"
                    multiple
                    accept="image/jpeg,image/png,image/webp,image/bmp,image/*"
                    aria-label="Upload reference photos"
                    className="absolute inset-0 z-10 cursor-pointer opacity-0"
                    onChange={e => {
                      takeFiles(e.target.files);
                      e.target.value = "";
                    }}
                  />
                </div>
              )}
              {files.length > 0 && (
                <div className="mt-4">
                  <ul className="grid grid-cols-5 gap-2">
                    {files.map((item, index) => (
                      <li key={`${item.name}-${item.size}-${index}`} className="relative">
                        <img src={previews[index]} alt={item.name} className="h-16 w-full rounded-lg object-cover" />
                        <button
                          type="button"
                          onClick={() => removeAt(index)}
                          aria-label={`Remove ${item.name}`}
                          className="absolute right-1 top-1 rounded-full bg-zinc-950/80 p-1 text-zinc-200 hover:text-white"
                        >
                          <X size={12}/>
                        </button>
                      </li>
                    ))}
                  </ul>
                  <p className="mt-2 text-xs text-zinc-500">
                    {files.length === 1
                      ? `${files[0].name} is used on every scene.`
                      : `${files.length} photos. Scenes take them in order. Extra scenes reuse the last photo.`}
                  </p>
                </div>
              )}
            </div>
          </section>
          <aside className="h-fit rounded-2xl border border-zinc-800 bg-zinc-900 p-6">
            <h2 className="font-semibold">Ready to create</h2>
            <div className="mt-5 space-y-4 text-sm">
              <div className="flex justify-between"><span className="text-zinc-500">Type</span><span>{type}</span></div>
              <div className="flex justify-between"><span className="text-zinc-500">Source</span><span>{input}</span></div>
              <div className="flex justify-between"><span className="text-zinc-500">Duration</span><span>{duration} sec</span></div>
              <div className="flex justify-between"><span className="text-zinc-500">Format</span><span>{ratio}</span></div>
              <div className="flex justify-between"><span className="text-zinc-500">Quality</span><span className="capitalize">{quality}</span></div>
            </div>
            <button disabled={!ready || (needsPhoto && files.length === 0)} onClick={() => setStep(2)} className="mt-7 flex w-full items-center justify-center gap-2 rounded-xl bg-violet-500 px-5 py-3.5 font-semibold hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-40">
              Continue <ArrowRight size={18}/>
            </button>
            {!ready && <p className="mt-3 text-center text-xs text-zinc-600">Describe your idea to continue.</p>}
            {ready && needsPhoto && files.length === 0 && <p className="mt-3 text-center text-xs text-amber-200/90">Add one or more reference photos. One photo is used on every scene.</p>}
          </aside>
        </div>
      </div>
    </main>
  );
}

// useSearchParams() opts the subtree into client rendering, which the static
// prerender of /create needs a Suspense boundary to tolerate.
export default function CreatePage() {
  return (
    <Suspense fallback={<main className="min-h-screen bg-zinc-950" />}>
      <CreateForm />
    </Suspense>
  );
}
