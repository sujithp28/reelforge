"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy, ImagePlus, Loader2, RefreshCw } from "lucide-react";
import {
  api, mediaUrl, supportedImage, uploadWithProgress,
  type LuxuryScript, type Project, type Scene,
} from "../lib/api";

const GOLD = "#C6A15B";
const NAVY = "#1B2838";
const IVORY = "#F7F4EF";
const BEIGE = "#E7DCC8";

type Props = {
  project: Project;
  busy: string | null;
  run: (key: string, fn: () => Promise<Project>) => void;
};

export function LuxuryStudio({ project, busy, run }: Props) {
  const script = project.content_script ?? null;
  const [draft, setDraft] = useState<LuxuryScript | null>(script);
  const [notice, setNotice] = useState<string | null>(null);
  const [targetId, setTargetId] = useState(project.scenes[0]?.id ?? "");
  const [uploads, setUploads] = useState<{ name: string; progress: number; preview: string }[]>([]);
  const [copied, setCopied] = useState<string | null>(null);
  const ensured = useRef<string | null>(null);
  const images = project.assets.filter(asset => asset.kind === "image");

  const incoming = JSON.stringify(project.content_script ?? null);
  useEffect(() => {
    setDraft(incoming === "null" ? null : JSON.parse(incoming) as LuxuryScript);
  }, [project.id, incoming]);

  useEffect(() => {
    const ready = (project.content_script?.image_prompts?.scenes.length ?? 0) === 5;
    if (ready || ensured.current === project.id) return;
    ensured.current = project.id;
    run("luxury-prompts", () => api.saveLuxuryScript(project.id, { apply_to_scenes: false }));
  }, [project.id, project.content_script, run]);

  useEffect(() => {
    let cancelled = false;
    api.luxuryCapabilities().then(body => {
      if (!cancelled) setNotice(body.available ? null : body.message);
    }).catch(() => {
      if (!cancelled) {
        setNotice("Image generation is not configured. Upload photos, or choose images already in this project.");
      }
    });
    return () => { cancelled = true; };
  }, [project.id]);

  if (!draft) {
    return (
      <section className="mb-8 rounded-2xl border p-6 text-sm" style={{ borderColor: GOLD, background: NAVY, color: IVORY }}>
        This Luxury Interiors reel has no script yet.
      </section>
    );
  }

  const language = draft.language === "te" ? "te" : "en";
  const block = language === "te" ? draft.telugu : draft.english;

  function updateBlock(next: LuxuryScript["english"]) {
    setDraft(current => current ? { ...current, [language]: next } : current);
  }

  function save(regenerate = false) {
    if (!draft) return;
    run(regenerate ? "luxury-regen" : "luxury-script", () => api.saveLuxuryScript(project.id, {
      topic: draft.topic,
      language: draft.language,
      brand_name: draft.brand_name,
      english: draft.english,
      telugu: draft.telugu,
      instagram_caption: draft.instagram_caption,
      youtube_description: draft.youtube_description,
      hashtags: draft.hashtags,
      image_prompts: draft.image_prompts,
      regenerate,
      apply_to_scenes: true,
    }));
  }

  async function copyText(key: string, text: string) {
    let copiedOk = false;
    try {
      await navigator.clipboard.writeText(text);
      copiedOk = true;
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      copiedOk = document.execCommand("copy");
      area.remove();
    }
    if (!copiedOk) {
      setNotice("The prompt could not be copied. Select the text and copy it manually.");
      return;
    }
    setNotice(current => current?.includes("could not be copied") ? null : current);
    setCopied(key);
    window.setTimeout(() => setCopied(current => current === key ? null : current), 1500);
  }

  function promptPack() {
    return draft?.image_prompts?.scenes ?? [];
  }

  function allPromptsText() {
    const format = draft?.image_prompts?.format ?? "Portrait 9:16, preferably 1080×1920 or higher.";
    const lines = [format, ""];
    promptPack().forEach((scene, index) => {
      lines.push(`Scene ${index + 1} — ${scene.role}`);
      lines.push(scene.prompt);
      lines.push("");
    });
    return lines.join("\n").trim();
  }

  function regeneratePrompt(index: number) {
    if (!draft) return;
    run(`prompt-${index}`, () => api.saveLuxuryScript(project.id, {
      topic: draft.topic,
      language: draft.language,
      brand_name: draft.brand_name,
      image_prompts: draft.image_prompts,
      regenerate_image_prompt: index,
      apply_to_scenes: false,
    }));
  }

  function savePrompts() {
    if (!draft?.image_prompts) return;
    run("luxury-prompts-save", () => api.saveLuxuryScript(project.id, {
      topic: draft.topic,
      language: draft.language,
      brand_name: draft.brand_name,
      image_prompts: draft.image_prompts,
      apply_to_scenes: false,
    }));
  }

  async function addFiles(list: File[]) {
    const accepted = list.filter(supportedImage);
    const rejected = list.length - accepted.length;
    if (rejected) {
      setNotice("Use JPEG, PNG, WebP, or BMP photos.");
    }
    if (!accepted.length) return;
    const previews = accepted.map(file => ({
      name: file.name,
      progress: 0,
      preview: URL.createObjectURL(file),
    }));
    setUploads(previews);
    try {
      let latest: Project | null = null;
      for (let i = 0; i < accepted.length; i++) {
        latest = await uploadWithProgress(project.id, accepted[i], {
          replan: i === accepted.length - 1,
          onProgress: fraction => {
            setUploads(current => current.map((item, index) => (
              index === i ? { ...item, progress: fraction } : item
            )));
          },
        });
      }
      if (latest) run("luxury-refresh", async () => latest as Project);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "That photo could not be added.");
    } finally {
      for (const item of previews) URL.revokeObjectURL(item.preview);
      setUploads([]);
    }
  }

  const scenesNeedingImages = project.scenes.flatMap((scene, index) => (
    scene.asset_id ? [] : [index + 1]
  ));
  const imageGap = scenesNeedingImages.length === 0
    ? "All five scenes have an image."
    : scenesNeedingImages.length === 1
      ? `Scene ${scenesNeedingImages[0]} still needs an image. Upload or assign a photo before rendering.`
      : `Scenes ${scenesNeedingImages.join(", ")} still need an image. Upload or assign a photo before rendering.`;

  return (
    <section className="mb-8 rounded-2xl border p-4 sm:p-6" style={{ borderColor: GOLD, background: NAVY, color: IVORY }}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="font-semibold">Luxury Interiors</h2>
          <p className="mt-1 text-sm" style={{ color: BEIGE }}>
            Five scenes, vertical 9:16. Music stays off until you add a track. The brand stays in the closing title.
          </p>
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={() => setDraft({ ...draft, language: "en" })} className="rounded-lg px-3 py-1.5 text-sm" style={language === "en" ? { background: GOLD, color: NAVY } : { border: `1px solid ${GOLD}` }}>English</button>
          <button type="button" onClick={() => setDraft({ ...draft, language: "te" })} className="rounded-lg px-3 py-1.5 text-sm" style={language === "te" ? { background: GOLD, color: NAVY } : { border: `1px solid ${GOLD}` }}>Telugu</button>
        </div>
      </div>

      <div className="mt-5 grid gap-4 md:grid-cols-[1fr_180px]">
        <label className="block text-sm">
          Brand name
          <input
            value={draft.brand_name}
            maxLength={80}
            onChange={e => setDraft({ ...draft, brand_name: e.target.value })}
            className="mt-1 w-full rounded-lg border bg-transparent px-3 py-2 outline-none"
            style={{ borderColor: GOLD }}
          />
        </label>
        <div className="text-sm">
          <p>Logo</p>
          {project.logo_url && (
            <img src={mediaUrl(project.logo_url) ?? ""} alt="" className="mt-2 h-12 w-12 rounded object-contain" style={{ background: BEIGE }} />
          )}
          <label className="mt-2 inline-flex cursor-pointer items-center gap-2 text-xs" style={{ color: BEIGE }}>
            {busy === "logo" ? <Loader2 size={14} className="animate-spin" /> : <ImagePlus size={14} />}
            {project.logo_url ? "Replace logo" : "Add logo"}
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp,image/bmp"
              className="hidden"
              onChange={e => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (!file) return;
                if (!supportedImage(file)) {
                  setNotice("Use a JPEG, PNG, WebP, or BMP logo.");
                  return;
                }
                run("logo", () => api.uploadLogo(project.id, file));
              }}
            />
          </label>
          <p className="mt-1 text-xs" style={{ color: BEIGE }}>Shown here only. It is not placed over the interiors.</p>
        </div>
      </div>

      <label className="mt-4 block text-sm">
        Topic
        <input
          value={draft.topic}
          maxLength={160}
          onChange={e => setDraft({ ...draft, topic: e.target.value })}
          className="mt-1 w-full rounded-lg border bg-transparent px-3 py-2 outline-none"
          style={{ borderColor: GOLD }}
        />
      </label>

      <label className="mt-4 block text-sm">
        Opening hook
        <input
          value={block.hook}
          maxLength={120}
          onChange={e => updateBlock({ ...block, hook: e.target.value })}
          className="mt-1 w-full rounded-lg border bg-transparent px-3 py-2 outline-none"
          style={{ borderColor: GOLD }}
        />
      </label>

      <div className="mt-4 space-y-3">
        {block.scenes.map((scene, index) => (
          <div key={`${scene.role}-${index}`} className="rounded-xl border p-3" style={{ borderColor: "rgba(198,161,91,0.45)" }}>
            <p className="text-xs" style={{ color: GOLD }}>{scene.role}</p>
            <input
              aria-label={`${scene.role} title`}
              value={scene.title}
              maxLength={80}
              onChange={e => {
                const scenes = block.scenes.map((item, i) => i === index ? { ...item, title: e.target.value } : item);
                updateBlock({ ...block, scenes });
              }}
              className="mt-2 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
              style={{ borderColor: GOLD }}
            />
            <input
              aria-label={`${scene.role} caption`}
              value={scene.subtitle}
              maxLength={120}
              onChange={e => {
                const scenes = block.scenes.map((item, i) => i === index ? { ...item, subtitle: e.target.value } : item);
                updateBlock({ ...block, scenes });
              }}
              className="mt-2 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
              style={{ borderColor: GOLD }}
            />
          </div>
        ))}
      </div>

      <label className="mt-4 block text-sm">
        Closing call to action
        <input
          value={block.cta}
          maxLength={160}
          onChange={e => updateBlock({ ...block, cta: e.target.value })}
          className="mt-1 w-full rounded-lg border bg-transparent px-3 py-2 outline-none"
          style={{ borderColor: GOLD }}
        />
      </label>
      <label className="mt-4 block text-sm">
        Instagram caption
        <textarea
          value={draft.instagram_caption}
          maxLength={500}
          onChange={e => setDraft({ ...draft, instagram_caption: e.target.value })}
          className="mt-1 min-h-20 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
          style={{ borderColor: GOLD }}
        />
      </label>
      <label className="mt-4 block text-sm">
        YouTube Shorts description
        <textarea
          value={draft.youtube_description}
          maxLength={1200}
          onChange={e => setDraft({ ...draft, youtube_description: e.target.value })}
          className="mt-1 min-h-28 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
          style={{ borderColor: GOLD }}
        />
      </label>
      <label className="mt-4 block text-sm">
        Hashtags
        <input
          value={draft.hashtags.join(" ")}
          onChange={e => setDraft({
            ...draft,
            hashtags: e.target.value.split(/\s+/).filter(Boolean).slice(0, 12),
          })}
          className="mt-1 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
          style={{ borderColor: GOLD }}
        />
      </label>
      <p className="mt-3 text-xs" style={{ color: BEIGE }}>{draft.note}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" disabled={busy === "luxury-script"} onClick={() => save(false)} className="rounded-lg px-4 py-2 text-sm font-semibold" style={{ background: GOLD, color: NAVY }}>
          Save script
        </button>
        <button
          type="button"
          disabled={busy === "luxury-regen"}
          onClick={() => {
            if (!window.confirm("Replace the script and image prompts with a new draft from the topic? Your caption edits will be lost.")) return;
            save(true);
          }}
          className="rounded-lg border px-4 py-2 text-sm"
          style={{ borderColor: GOLD }}
        >
          Regenerate from topic
        </button>
      </div>

      <div className="mt-8 border-t pt-5" style={{ borderColor: "rgba(198,161,91,0.45)" }}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="font-semibold">Image prompts</h3>
            <p className="mt-1 max-w-3xl text-sm" style={{ color: BEIGE }}>
              {draft.image_prompts?.instruction
                ?? "Copy each prompt and generate its image using your preferred image-generation tool. Download the generated image and upload it into the matching scene."}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void copyText("all", allPromptsText())}
            className="inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
            style={{ borderColor: GOLD }}
          >
            {copied === "all" ? <Check size={14} /> : <Copy size={14} />}
            {copied === "all" ? "Copied" : "Copy all prompts"}
          </button>
        </div>
        <p className="mt-3 text-sm font-medium" style={{ color: GOLD }}>
          {draft.image_prompts?.format ?? "Portrait 9:16, preferably 1080×1920 or higher."}
        </p>
        <p className="mt-1 text-xs" style={{ color: BEIGE }}>
          Regenerating one card rewrites only that prompt, using the topic and language selected above.
        </p>
        <p className="mt-2 text-sm" style={{ color: scenesNeedingImages.length === 0 ? GOLD : BEIGE }}>
          {imageGap}
        </p>
        {(draft.image_prompts?.scenes.length ?? 0) !== 5 ? (
          <p className="mt-4 text-sm" style={{ color: BEIGE }}>Preparing the five prompts…</p>
        ) : (
          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            {draft.image_prompts?.scenes.map((item, index) => {
              const scene = project.scenes[index];
              const preview = mediaUrl(scene?.asset_url ?? null);
              return (
                <article key={item.role} className="rounded-xl border p-3" style={{ borderColor: "rgba(198,161,91,0.45)" }}>
                  <p className="text-xs" style={{ color: GOLD }}>Scene {index + 1} · {item.role}</p>
                  <textarea
                    aria-label={`${item.role} image prompt`}
                    value={item.prompt}
                    maxLength={800}
                    onChange={e => {
                      const scenes = draft.image_prompts?.scenes.map((row, rowIndex) => (
                        rowIndex === index ? { ...row, prompt: e.target.value } : row
                      )) ?? [];
                      setDraft({
                        ...draft,
                        image_prompts: draft.image_prompts
                          ? { ...draft.image_prompts, scenes }
                          : draft.image_prompts,
                      });
                    }}
                    className="mt-2 min-h-28 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
                    style={{ borderColor: GOLD }}
                  />
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => void copyText(`prompt-${index}`, item.prompt)}
                      className="inline-flex items-center gap-1 rounded-lg border px-3 py-1.5 text-xs"
                      style={{ borderColor: GOLD }}
                    >
                      {copied === `prompt-${index}` ? <Check size={12} /> : <Copy size={12} />}
                      {copied === `prompt-${index}` ? "Copied" : "Copy prompt"}
                    </button>
                    <button
                      type="button"
                      disabled={busy === `prompt-${index}`}
                      onClick={() => regeneratePrompt(index)}
                      className="inline-flex items-center gap-1 rounded-lg border px-3 py-1.5 text-xs"
                      style={{ borderColor: GOLD }}
                    >
                      <RefreshCw size={12} />
                      Regenerate prompt
                    </button>
                  </div>
                  <div className="mt-3 flex items-end gap-3">
                    <div>
                      <div className="w-16 overflow-hidden rounded-lg bg-black/30" style={{ aspectRatio: "9 / 16" }}>
                        {preview
                          ? <img src={preview} alt="" className="h-full w-full object-cover" />
                          : <span className="flex h-full items-center px-1 text-center text-[10px]" style={{ color: BEIGE }}>9:16</span>}
                      </div>
                      <p className="mt-1 text-[10px]" style={{ color: preview ? GOLD : BEIGE }}>
                        {preview ? "Image assigned" : "Needs an image"}
                      </p>
                    </div>
                    {scene && (
                      <label className="inline-flex cursor-pointer items-center gap-2 text-xs">
                        <ImagePlus size={14} />
                        {scene.asset_id ? "Replace image" : "Upload image"}
                        <input
                          type="file"
                          accept="image/jpeg,image/png,image/webp,image/bmp"
                          aria-label={`Upload image for ${item.role}`}
                          className="hidden"
                          onChange={e => {
                            const file = e.target.files?.[0];
                            e.target.value = "";
                            if (!file) return;
                            if (!supportedImage(file)) {
                              setNotice("Use a JPEG, PNG, WebP, or BMP photo.");
                              return;
                            }
                            run(`scene-photo-${scene.id}`, () => api.upload(project.id, file, scene.id, { replan: false }));
                          }}
                        />
                      </label>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}
        <button
          type="button"
          disabled={busy === "luxury-prompts-save"}
          onClick={savePrompts}
          className="mt-4 rounded-lg px-4 py-2 text-sm font-semibold"
          style={{ background: GOLD, color: NAVY }}
        >
          Save prompts
        </button>
      </div>

      <div className="mt-8 border-t pt-5" style={{ borderColor: "rgba(198,161,91,0.45)" }}>
        <h3 className="font-semibold">Pictures</h3>
        <p className="mt-1 text-sm" style={{ color: BEIGE }}>
          Choose a photo already in this project, or upload new ones. Uploads are reused, not copied again.
        </p>
        <label className="mt-3 block text-sm">
          Assign to
          <select
            value={targetId}
            onChange={e => setTargetId(e.target.value)}
            className="mt-1 w-full rounded-lg border bg-transparent px-3 py-2 text-sm outline-none"
            style={{ borderColor: GOLD }}
          >
            {project.scenes.map((scene: Scene, index) => (
              <option key={scene.id} value={scene.id} style={{ color: NAVY }}>
                Scene {index + 1}: {scene.text_title || scene.title}
              </option>
            ))}
          </select>
        </label>
        {images.length === 0 ? (
          <p className="mt-4 text-sm" style={{ color: BEIGE }}>
            {notice ?? "Upload photos, or choose images already in this project."}
          </p>
        ) : (
          <div className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-5">
            {images.map(asset => (
              <button
                key={asset.id}
                type="button"
                onClick={() => {
                  if (!targetId) return;
                  run(`assign-${asset.id}`, () => api.assignSceneAsset(project.id, targetId, asset.id));
                }}
                className="overflow-hidden rounded-lg border text-left"
                style={{ borderColor: GOLD }}
              >
                <img src={mediaUrl(asset.url) ?? ""} alt="" className="aspect-square w-full object-cover" />
                <span className="block truncate px-1 py-1 text-[11px]" style={{ color: BEIGE }}>{asset.filename}</span>
              </button>
            ))}
          </div>
        )}
        <label className="mt-4 flex cursor-pointer items-center gap-2 text-sm">
          <ImagePlus size={16} /> Upload photos
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp,image/bmp"
            multiple
            className="hidden"
            onChange={e => {
              const list = Array.from(e.target.files ?? []);
              e.target.value = "";
              void addFiles(list);
            }}
          />
        </label>
        {uploads.length > 0 && (
          <ul className="mt-3 space-y-2">
            {uploads.map(item => (
              <li key={item.name} className="flex items-center gap-3 text-xs">
                <img src={item.preview} alt="" className="h-10 w-10 rounded object-cover" />
                <span className="w-28 truncate">{item.name}</span>
                <span className="h-1.5 flex-1 overflow-hidden rounded" style={{ background: BEIGE }}>
                  <span className="block h-full" style={{ width: `${Math.round(item.progress * 100)}%`, background: GOLD }} />
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-4 text-sm" style={{ color: BEIGE }}>
          {notice ?? "Choose a photo already in this project, or upload one onto a scene above."}
        </p>
      </div>
    </section>
  );
}
