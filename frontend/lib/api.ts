// Single place the frontend talks to FastAPI. The starter had a second copy
// of the storyboard planner inline in the create page; this replaces it.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

/** JSON calls should fail visibly instead of leaving the UI on a spinner. */
const API_TIMEOUT_MS = 20_000;

export const CATEGORIES = [
  "Cinematic", "Product", "Business", "Real Estate",
  "Personal", "Social Media", "Event", "Creative", "Other",
  "Luxury Interiors",
] as const;

export const LUXURY_CATEGORY = "Luxury Interiors";
export const LUXURY_BRAND = "Luxury Living Studio";

export const RATIOS = ["9:16", "16:9", "1:1", "4:5"] as const;
/** Display values for the export studio. They match backend/app/export_presets.py. */
export const EXPORT_FORMATS = [
  { id: "instagram_reel", label: "Instagram Reel", ratio: "9:16", width: 1080, height: 1920, fps: 30 },
  { id: "youtube_short", label: "YouTube Short", ratio: "9:16", width: 1080, height: 1920, fps: 30 },
  { id: "instagram_feed", label: "Instagram Feed", ratio: "4:5", width: 1080, height: 1350, fps: 30 },
  { id: "youtube_landscape", label: "YouTube", ratio: "16:9", width: 1920, height: 1080, fps: 30 },
] as const;
export const INPUT_TYPES = ["Idea", "Image", "Image + text", "Product", "Person"] as const;
export const DURATIONS = [15, 30, 45, 60] as const;

/** Customer-facing quality. Real providers map this to their own settings. */
export const QUALITIES = [
  { value: "standard", label: "Standard", hint: "Motion from your images" },
  { value: "high", label: "High", hint: "Motion from your images" },
] as const;

/** Shown when the server is on the mock FFmpeg renderer. */
export const MOCK_QUALITY_NOTE =
  "This preview is built with FFmpeg from your photos or title cards. Standard and High leave the resolution and the pictures unchanged, and this renderer does not generate AI video.";

export type Quality = (typeof QUALITIES)[number]["value"];

/** Per-scene generation state. `failed` is the one the customer can retry. */
export type ClipStatus = "pending" | "ready" | "failed";

export type Ratio = (typeof RATIOS)[number];
export type ExportFormat = (typeof EXPORT_FORMATS)[number]["id"];
export type ProjectStatus = "draft" | "rendering" | "ready" | "failed";

export type Scene = {
  id: string;
  position: number;
  title: string;
  prompt: string;
  duration: number;
  caption: string | null;
  /** On-screen title. Separate from the scene's beat name. */
  text_title: string | null;
  text_subtitle: string | null;
  show_title: boolean;
  show_subtitle: boolean;
  asset_id: string | null;
  asset_url: string | null;
  /** How many times this scene's prompt has been regenerated. */
  regen_count: number;
  /** Whether this scene's video has been generated yet. */
  clip_status: ClipStatus;
  /** Customer-safe reason this scene failed, if it did. */
  clip_error: string | null;
  clip_attempts: number;
};

export type Asset = { id: string; kind: "image" | "audio" | "logo"; filename: string; url: string };

export type LuxurySceneCopy = {
  role: string;
  title: string;
  subtitle: string;
  narration: string;
};

export type LuxuryLanguageCopy = {
  hook: string;
  cta: string;
  scenes: LuxurySceneCopy[];
};

export type LuxuryImagePrompt = {
  role: string;
  prompt: string;
  variant: number;
};

export type LuxuryImagePrompts = {
  format: string;
  instruction: string;
  scenes: LuxuryImagePrompt[];
};

export type LuxuryScript = {
  template: string;
  topic: string;
  language: "en" | "te";
  brand_name: string;
  english: LuxuryLanguageCopy;
  telugu: LuxuryLanguageCopy;
  instagram_caption: string;
  youtube_description: string;
  hashtags: string[];
  note: string;
  image_prompts?: LuxuryImagePrompts;
};

export type Project = {
  id: string;
  title: string;
  idea: string;
  category: string;
  input_type: string;
  duration: number;
  aspect_ratio: Ratio;
  export_preset: ExportFormat | null;
  status: ProjectStatus;
  progress: number;
  error: string | null;
  video_url: string | null;
  total_duration: number;
  music_volume: number;
  music_fade_out: number;
  music_fade_in: number;
  music_enabled: boolean;
  /** True when the saved MP4 no longer matches the current scenes. */
  output_stale: boolean;
  quality: Quality;
  brand_name?: string | null;
  content_script?: LuxuryScript | null;
  logo_asset_id?: string | null;
  logo_url?: string | null;
  created_at: string;
  updated_at: string;
  scenes: Scene[];
  assets: Asset[];
  job: {
    id: string;
    status: string;
  } | null;
};

export type ProjectSummary = Omit<
  Project,
  "scenes" | "assets" | "total_duration" | "job"
> & { scene_count: number; cover_url?: string | null };

/** Backend limits, mirrored so the UI can stop an edit before it round-trips. */
export const MIN_REEL_SECONDS = 5;
export const MAX_REEL_SECONDS = 120;
export const MAX_SCENE_SECONDS = 30;

/** Turn a `/media/...` path from the API into something an <img>/<video> can load. */
export function mediaUrl(path: string | null): string | null {
  if (!path) return null;
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

export function selectedExportFormat(project: {
  export_preset?: string | null;
  aspect_ratio: string;
}): ExportFormat | "" {
  if (EXPORT_FORMATS.some(item => item.id === project.export_preset)) {
    return project.export_preset as ExportFormat;
  }
  if (project.aspect_ratio === "16:9") return "youtube_landscape";
  if (project.aspect_ratio === "4:5") return "instagram_feed";
  if (project.aspect_ratio === "9:16") return "instagram_reel";
  return "";
}

export function exportFormatLabel(id: string | null | undefined): string | null {
  return EXPORT_FORMATS.find(item => item.id === id)?.label ?? null;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

function unreachable(error: unknown): ApiError {
  const name = error instanceof Error ? error.name : "";
  if (name === "TimeoutError" || name === "AbortError") {
    return new ApiError(
      `The ReelForge API at ${API_BASE} did not respond within ${API_TIMEOUT_MS / 1000}s.`,
      0,
    );
  }
  return new ApiError(
    `Cannot reach the ReelForge API at ${API_BASE}. Is the backend running?`,
    0,
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  const timeout = AbortSignal.timeout(API_TIMEOUT_MS);
  const signal = init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal,
      headers:
        init?.body instanceof FormData
          ? init?.headers
          : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch (error) {
    throw unreachable(error);
  }

  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      // FastAPI puts a string in `detail`, or a list of validation errors.
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(detail, res.status);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  health: () =>
    request<{ status: string; ffmpeg: boolean; video_provider: string }>("/health"),

  listProjects: (limit = 100, offset = 0) =>
    request<{ projects: ProjectSummary[]; total: number }>(
      `/api/projects?limit=${limit}&offset=${offset}`,
    ).then((r) => r.projects),

  getProject: (id: string) => request<Project>(`/api/projects/${id}`),

  createProject: (body: {
    idea: string;
    category: string;
    input_type: string;
    duration: number;
    aspect_ratio: string;
    quality: string;
    export_preset?: string;
    brand_name?: string;
    language?: string;
  }) => request<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),

  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: "DELETE" }),

  renameProject: (id: string, title: string) =>
    request<Project>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),

  reorderScenes: (projectId: string, sceneIds: string[]) =>
    request<Project>(`/api/projects/${projectId}/scenes/order`, {
      method: "PUT",
      body: JSON.stringify({ scene_ids: sceneIds }),
    }),

  addScene: (projectId: string) =>
    request<Project>(`/api/projects/${projectId}/scenes`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  deleteScene: (projectId: string, sceneId: string) =>
    request<Project>(`/api/projects/${projectId}/scenes/${sceneId}`, { method: "DELETE" }),

  deleteAsset: (projectId: string, assetId: string) =>
    request<Project>(`/api/projects/${projectId}/assets/${assetId}`, { method: "DELETE" }),

  removeScenePhoto: (projectId: string, sceneId: string) =>
    request<Project>(`/api/projects/${projectId}/scenes/${sceneId}/asset`, { method: "DELETE" }),

  updateScene: (
    projectId: string,
    sceneId: string,
    patch: Partial<Pick<Scene, "title" | "prompt" | "caption" | "duration" | "text_title" | "text_subtitle" | "show_title" | "show_subtitle">>,
  ) =>
    request<Project>(`/api/projects/${projectId}/scenes/${sceneId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  regenerateScene: (projectId: string, sceneId: string) =>
    request<Project>(`/api/projects/${projectId}/scenes/${sceneId}/regenerate`, {
      method: "POST",
    }),

  /** Point one scene at an image already stored on this project. */
  assignSceneAsset: (
    projectId: string,
    sceneId: string,
    assetId: string,
    options?: { replan?: boolean },
  ) => {
    const query = options?.replan ? "?replan=true" : "";
    return request<Project>(`/api/projects/${projectId}/scenes/${sceneId}/asset${query}`, {
      method: "PUT",
      body: JSON.stringify({ asset_id: assetId }),
    });
  },

  upload: (
    projectId: string,
    file: File,
    sceneId?: string,
    options?: { replan?: boolean; append?: boolean },
  ) => {
    const form = new FormData();
    form.append("file", file);
    const params = new URLSearchParams();
    if (sceneId) params.set("scene_id", sceneId);
    // False defers planning until the last image of this customer batch.
    if (options?.replan === false) params.set("replan", "false");
    if (options?.append) params.set("append", "true");
    const query = params.toString() ? `?${params}` : "";
    return request<Project>(`/api/projects/${projectId}/uploads${query}`, {
      method: "POST",
      body: form,
    });
  },

  updateQuality: (projectId: string, quality: Quality) =>
    request<Project>(`/api/projects/${projectId}/quality`, {
      method: "PATCH",
      body: JSON.stringify({ quality }),
    }),

  updateExport: (projectId: string, exportPreset: ExportFormat) =>
    request<Project>(`/api/projects/${projectId}/export`, {
      method: "PATCH",
      body: JSON.stringify({ export_preset: exportPreset }),
    }),

  /**
   * Regenerate one scene and reassemble. Scenes that already succeeded keep
   * their clips, so this costs one scene rather than the whole reel.
   */
  retryScene: (projectId: string, sceneId: string) =>
    request<{ job_id: string; scene_id: string; status: string }>(
      `/api/projects/${projectId}/scenes/${sceneId}/retry`,
      { method: "POST" },
    ),

  cancelJob: (projectId: string, jobId: string) =>
    request<{ job_id: string; cancel_requested: boolean }>(
      `/api/projects/${projectId}/jobs/${jobId}/cancel`,
      { method: "POST" },
    ),

  updateAudio: (
    projectId: string,
    settings: {
      music_volume?: number;
      music_fade_out?: number;
      music_fade_in?: number;
      music_enabled?: boolean;
    },
  ) =>
    request<Project>(`/api/projects/${projectId}/audio`, {
      method: "PATCH",
      body: JSON.stringify(settings),
    }),

  render: (projectId: string) =>
    request<{ id: string; job_id: string; status: string }>(
      `/api/projects/${projectId}/render`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  luxuryCapabilities: () =>
    request<{ available: boolean; message: string }>("/api/luxury/capabilities"),

  saveLuxuryScript: (
    projectId: string,
    body: Partial<LuxuryScript> & {
      regenerate?: boolean;
      apply_to_scenes?: boolean;
      regenerate_image_prompt?: number;
    },
  ) =>
    request<Project>(`/api/projects/${projectId}/luxury-script`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  uploadLogo: (projectId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<Project>(`/api/projects/${projectId}/logo`, { method: "POST", body: form });
  },
};

const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/bmp"]);

export function supportedImage(file: File): boolean {
  return IMAGE_TYPES.has(file.type) || /\.(jpe?g|png|webp|bmp)$/i.test(file.name);
}

/** Upload one photo and report bytes sent, for the luxury template batch. */
export function uploadWithProgress(
  projectId: string,
  file: File,
  options?: { replan?: boolean; onProgress?: (fraction: number) => void },
): Promise<Project> {
  const form = new FormData();
  form.append("file", file);
  const params = new URLSearchParams();
  if (options?.replan === false) params.set("replan", "false");
  const query = params.toString() ? `?${params}` : "";
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/projects/${projectId}/uploads${query}`);
    xhr.timeout = 60_000;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        options?.onProgress?.(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as Project);
        return;
      }
      let detail = `Request failed (${xhr.status})`;
      try {
        const body = JSON.parse(xhr.responseText) as { detail?: string };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        /* keep the status */
      }
      reject(new ApiError(detail, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError(`Cannot reach the ReelForge API at ${API_BASE}.`, 0));
    xhr.ontimeout = () =>
      reject(new ApiError(`The ReelForge API at ${API_BASE} did not respond within 60s.`, 0));
    xhr.send(form);
  });
}
