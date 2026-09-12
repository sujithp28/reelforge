import Link from "next/link";
import { ArrowRight, Clapperboard, Sparkles, WandSparkles } from "lucide-react";

const types = [
  ["🎬", "Cinematic"], ["🛍️", "Product"], ["🏢", "Business"],
  ["🏠", "Real Estate"], ["👤", "Personal"], ["📱", "Social Media"],
  ["🎉", "Event"], ["✨", "Creative"], ["＋", "Other"],
];

export default function Home() {
  return (
    <main className="min-h-screen bg-zinc-950">
      <nav className="mx-auto flex max-w-7xl items-center justify-between px-6 py-6">
        <div className="text-xl font-bold tracking-tight">REEL<span className="text-violet-400">FORGE</span></div>
        <Link href="/create" className="rounded-full bg-white px-5 py-2.5 text-sm font-semibold text-zinc-950 hover:bg-zinc-200">
          Create a Reel
        </Link>
      </nav>

      <section className="mx-auto max-w-7xl px-6 pb-20 pt-20">
        <div className="max-w-3xl">
          <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-300">
            <Sparkles size={14} className="text-violet-400" /> AI reel creation, simplified
          </div>
          <h1 className="text-6xl font-semibold leading-[1.02] tracking-tight md:text-7xl">
            Your idea.<br /><span className="text-zinc-500">Your scenes.</span><br />Your reel.
          </h1>
          <p className="mt-7 max-w-2xl text-lg leading-8 text-zinc-400">
            Turn an idea, photo, product or person into a beautiful AI reel — with scenes, captions, voice and music.
          </p>
          <div className="mt-9 flex gap-3">
            <Link href="/create" className="inline-flex items-center gap-2 rounded-xl bg-violet-500 px-6 py-3.5 font-semibold text-white hover:bg-violet-400">
              Start creating <ArrowRight size={18} />
            </Link>
            <Link href="/dashboard" className="rounded-xl border border-zinc-800 px-6 py-3.5 font-semibold text-zinc-200 hover:bg-zinc-900">
              View dashboard
            </Link>
          </div>
        </div>
      </section>

      <section className="border-y border-zinc-900 bg-zinc-950/80">
        <div className="mx-auto max-w-7xl px-6 py-16">
          <div className="mb-8 flex items-center gap-3">
            <Clapperboard className="text-violet-400" />
            <div>
              <h2 className="text-xl font-semibold">Create anything</h2>
              <p className="text-sm text-zinc-500">Choose what best describes your reel. ReelForge handles the workflow.</p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {types.map(([emoji, name]) => (
              <Link key={name} href={`/create?type=${encodeURIComponent(name)}`} className="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5 transition hover:-translate-y-0.5 hover:border-zinc-700 hover:bg-zinc-900">
                <div className="text-2xl">{emoji}</div>
                <div className="mt-3 font-medium">{name}</div>
              </Link>
            ))}
          </div>
        </div>
      </section>
    </main>
  );
}
