"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Loader2, Play, Plus, Trash2 } from "lucide-react";
import { api, type ProjectSummary } from "../../lib/api";

const STATUS_STYLES: Record<string, string> = {
  draft: "border-zinc-700 text-zinc-400",
  rendering: "border-violet-600 bg-violet-500/10 text-violet-300",
  ready: "border-emerald-700 bg-emerald-500/10 text-emerald-300",
  failed: "border-red-800 bg-red-500/10 text-red-300",
};

export default function Dashboard() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load your reels.");
      setProjects([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Keep the list fresh while any reel is still rendering.
  useEffect(() => {
    if (!projects?.some(p => p.status === "rendering")) return;
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [projects, load]);

  async function remove(id: string, title: string) {
    if (!window.confirm(`Delete “${title}”? This also removes its uploads and rendered video.`)) return;
    try {
      await api.deleteProject(id);
      setProjects(current => current?.filter(p => p.id !== id) ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete that reel.");
    }
  }

  return (
    <main className="min-h-screen bg-zinc-950 px-6 py-8">
      <div className="mx-auto max-w-7xl">
        <header className="flex items-center justify-between">
          <div>
            <Link href="/" className="text-xl font-bold">REEL<span className="text-violet-400">FORGE</span></Link>
            <p className="mt-2 text-sm text-zinc-500">Your workspace</p>
          </div>
          <Link href="/create" className="flex items-center gap-2 rounded-xl bg-violet-500 px-5 py-3 text-sm font-semibold hover:bg-violet-400"><Plus size={17}/> New Reel</Link>
        </header>

        <section className="mt-12">
          <h1 className="text-3xl font-semibold">My Reels</h1>

          {error && (
            <p className="mt-6 flex items-center gap-3 rounded-xl border border-amber-900 bg-amber-950/40 p-4 text-sm text-amber-300">
              <AlertTriangle size={18} className="shrink-0"/> {error}
            </p>
          )}

          {projects === null && (
            <div className="mt-10 flex justify-center text-zinc-600"><Loader2 className="animate-spin"/></div>
          )}

          {projects?.length === 0 && !error && (
            <div className="mt-7 rounded-2xl border border-dashed border-zinc-800 p-12 text-center">
              <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-900"><Play size={20}/></div>
              <h2 className="mt-5 font-semibold">No reels yet</h2>
              <p className="mt-2 text-sm text-zinc-500">Create your first reel and it will appear here.</p>
              <Link href="/create" className="mt-6 inline-block rounded-xl bg-white px-5 py-3 text-sm font-semibold text-zinc-950">Create your first reel</Link>
            </div>
          )}

          {!!projects?.length && (
            <div className="mt-7 grid gap-3">
              {projects.map(p => (
                <div key={p.id} className="flex flex-wrap items-center gap-4 rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
                  <Link href={`/projects/${p.id}`} className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-3">
                      <h2 className="truncate font-semibold">{p.title}</h2>
                      <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${STATUS_STYLES[p.status]}`}>
                        {p.status === "rendering" ? `Rendering ${p.progress}%` : p.status}
                      </span>
                    </div>
                    <p className="mt-1.5 text-sm text-zinc-500">
                      {p.category} · {p.aspect_ratio} · {p.duration}s · {p.scene_count} scenes
                    </p>
                  </Link>
                  <div className="flex items-center gap-2">
                    <Link href={`/projects/${p.id}`} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm hover:bg-zinc-800">Open</Link>
                    <button onClick={() => remove(p.id, p.title)} aria-label={`Delete ${p.title}`} className="rounded-lg border border-zinc-800 p-2 text-zinc-500 hover:border-red-900 hover:text-red-400">
                      <Trash2 size={16}/>
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
