// Single place the frontend talks to FastAPI. The starter had a second copy
// of the storyboard planner inline in the create page; this replaces it.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export const CATEGORIES = [
  "Cinematic", "Product", "Business", "Real Estate",
  "Personal", "Social Media", "Event", "Creative", "Other",
] as const;

export const RATIOS = ["9:16", "16:9", "1:1", "4:5"] as const;
export const INPUT_TYPES = ["Idea", "Image", "Image + text", "Product", "Person"] as const;
export const DURATIONS = [15, 30, 45, 60] as const;

export type Ratio = (typeof RATIOS)[number];
export type ProjectStatus = "draft" | "rendering" | "ready" | "failed";

export type Scene = {
  id: string;
  position: number;
  title: string;
  prompt: string;
  duration: number;
  caption: string | null;
  asset_id: string | null;
  asset_url: string | null;
  /** How many times this scene's prompt has been regenerated. */
  regen_count: number;
};

export type Asset = { id: string; kind: "image" | "audio"; filename: string; url: string };

export type Project = {
  id: string;
  title: string;
  idea: string;
  category: string;
  input_type: string;
  duration: number;
  aspect_ratio: Ratio;
  status: ProjectStatus;
  progress: number;
  error: string | null;
  video_url: string | null;
  total_duration: number;
  music_volume: number;
  music_fade_out: number;
  created_at: string;
  updated_at: string;
  scenes: Scene[];
  assets: Asset[];
  job: { id: string; status: string; provider: string } | null;
};

export type ProjectSummary = Omit<
  Project,
  "scenes" | "assets" | "total_duration" | "job"
> & { scene_count: number };

/** Backend limits, mirrored so the UI can stop an edit before it round-trips. */
export const MIN_REEL_SECONDS = 5;
export const MAX_REEL_SECONDS = 120;
export const MAX_SCENE_SECONDS = 30;

/** Turn a `/media/...` path from the API into something an <img>/<video> can load. */
export function mediaUrl(path: string | null): string | null {
  if (!path) return null;
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers:
        init?.body instanceof FormData
          ? init?.headers
          : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    // A dead backend is the single most likely failure in local dev, so name it.
    throw new ApiError(
      `Cannot reach the ReelForge API at ${API_BASE}. Is the backend running?`,
      0,
    );
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
  health: () => request<{ status: string; ffmpeg: boolean }>("/health"),

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
  }) => request<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),

  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: "DELETE" }),

  updateScene: (
    projectId: string,
    sceneId: string,
    patch: Partial<Pick<Scene, "title" | "prompt" | "caption" | "duration">>,
  ) =>
    request<Project>(`/api/projects/${projectId}/scenes/${sceneId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  regenerateScene: (projectId: string, sceneId: string) =>
    request<Project>(`/api/projects/${projectId}/scenes/${sceneId}/regenerate`, {
      method: "POST",
    }),

  upload: (projectId: string, file: File, sceneId?: string) => {
    const form = new FormData();
    form.append("file", file);
    const query = sceneId ? `?scene_id=${encodeURIComponent(sceneId)}` : "";
    return request<Project>(`/api/projects/${projectId}/uploads${query}`, {
      method: "POST",
      body: form,
    });
  },

  updateAudio: (
    projectId: string,
    settings: { music_volume?: number; music_fade_out?: number },
  ) =>
    request<Project>(`/api/projects/${projectId}/audio`, {
      method: "PATCH",
      body: JSON.stringify(settings),
    }),

  /** `provider` defaults to the backend's configured generator (mock locally). */
  render: (projectId: string, provider?: string) =>
    request<{ id: string; job_id: string; status: string; provider: string }>(
      `/api/projects/${projectId}/render`,
      { method: "POST", body: JSON.stringify({ provider: provider ?? null }) },
    ),
};
