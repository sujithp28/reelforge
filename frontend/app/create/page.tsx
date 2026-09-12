"use client";

import { Suspense, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, ImagePlus, Loader2, Sparkles, Upload, X } from "lucide-react";
import {
  api, CATEGORIES, DURATIONS, INPUT_TYPES, QUALITIES, RATIOS,
  type Quality, type Ratio,
} from "../../lib/api";

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
  const [file, setFile] = useState<File | null>(null);
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ready = useMemo(() => idea.trim().length > 0, [idea]);

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      const project = await api.createProject({
        idea: idea.trim(),
        category: type,
        input_type: input,
        duration,
        aspect_ratio: ratio,
        quality,
      });
      // Uploads need a project to attach to, so the staged file goes up second.
      if (file) {
        try {
          await api.upload(project.id, file);
        } catch (e) {
          // The storyboard exists either way; don't lose it over a bad file.
          console.warn("reference upload failed", e);
        }
      }
      router.push(`/projects/${project.id}`);
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

          <button onClick={generate} disabled={busy} className="mt-8 inline-flex items-center gap-2 rounded-xl bg-violet-500 px-6 py-3.5 font-semibold hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-50">
            {busy ? <><Loader2 size={18} className="animate-spin"/> Building storyboard</> : <>Generate storyboard <Sparkles size={18}/></>}
          </button>
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

              {input !== "Idea" && !file && (
                <label className="mt-4 flex w-full cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-700 p-7 text-sm text-zinc-400 hover:bg-zinc-950">
                  <Upload size={18}/> Upload reference <ImagePlus size={18}/>
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp,image/bmp"
                    className="hidden"
                    onChange={e => setFile(e.target.files?.[0] ?? null)}
                  />
                </label>
              )}
              {file && (
                <div className="mt-4 flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950 p-4 text-sm">
                  <span className="truncate text-zinc-300">{file.name}</span>
                  <button onClick={() => setFile(null)} aria-label="Remove reference" className="ml-3 shrink-0 text-zinc-500 hover:text-white"><X size={16}/></button>
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
            <button disabled={!ready} onClick={() => setStep(2)} className="mt-7 flex w-full items-center justify-center gap-2 rounded-xl bg-violet-500 px-5 py-3.5 font-semibold hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-40">
              Continue <ArrowRight size={18}/>
            </button>
            {!ready && <p className="mt-3 text-center text-xs text-zinc-600">Describe your idea to continue.</p>}
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
