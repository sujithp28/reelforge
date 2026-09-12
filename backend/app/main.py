"""HTTP layer.

Routes validate input, call repo.py inside one transaction, and enqueue jobs.
No SQL, no ffmpeg, no filesystem paths live here.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, ffmpeg, jobs, providers, repo, storage, storyboard

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reelforge")

app = FastAPI(title="ReelForge API", version="1.1.0")

# The Next.js dev server runs on a different origin, so without this every
# browser request fails preflight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init()
with db.connect() as _conn:
    _released = repo.release_stale_renders(_conn)
if _released:
    log.warning("released %d render(s) orphaned by a restart", _released)

# Local storage is served by the app. With REELFORGE_STORAGE=s3 the keys are
# identical and url_for returns absolute URLs instead, so this mount becomes
# unnecessary rather than wrong.
if config.STORAGE_BACKEND == "local":
    app.mount(
        config.MEDIA_URL_PREFIX,
        StaticFiles(directory=config.DATA_DIR),
        name="media",
    )

ALLOWED_RATIOS = set(storyboard.ASPECT_SIZES)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".ogg"}


# --- request models ---------------------------------------------------------

class ProjectRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=2000)
    category: str = "Cinematic"
    input_type: str = "Idea"
    duration: int = Field(
        default=30, ge=config.MIN_REEL_SECONDS, le=config.MAX_REEL_SECONDS
    )
    aspect_ratio: str = "9:16"
    title: str | None = Field(default=None, max_length=120)


class StoryboardRequest(BaseModel):
    """Preview-only planner, kept for the original /api/storyboard endpoint."""
    idea: str
    category: str = "Cinematic"
    duration: int = Field(
        default=30, ge=config.MIN_REEL_SECONDS, le=config.MAX_REEL_SECONDS
    )
    aspect_ratio: str = "9:16"


class SceneUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=2000)
    caption: str | None = Field(default=None, max_length=200)
    duration: int | None = Field(
        default=None,
        ge=config.MIN_SCENE_SECONDS_ALLOWED,
        le=config.MAX_SCENE_SECONDS_ALLOWED,
    )


class AudioSettingsRequest(BaseModel):
    music_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    music_fade_out: int | None = Field(default=None, ge=0, le=10)


class RenderRequest(BaseModel):
    provider: str | None = None


# --- helpers ----------------------------------------------------------------

def _check_ratio(ratio: str) -> str:
    if ratio not in ALLOWED_RATIOS:
        raise HTTPException(422, f"aspect_ratio must be one of {sorted(ALLOWED_RATIOS)}")
    return ratio


def _require_project(conn, project_id: str) -> dict:
    project = repo.get_project(conn, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def _serialize(conn, project: dict) -> dict:
    """Build the project payload. This shape is the frontend contract."""
    out = dict(project)
    scenes = repo.list_scenes(conn, project["id"])
    assets = repo.list_assets(conn, project["id"])

    for asset in assets:
        asset["url"] = storage.storage.url_for(asset.pop("storage_key", None))
    by_id = {a["id"]: a for a in assets}
    for scene in scenes:
        asset = by_id.get(scene.get("asset_id"))
        scene["asset_url"] = asset["url"] if asset else None

    out["scenes"] = scenes
    out["assets"] = assets
    out["total_duration"] = sum(s["duration"] for s in scenes)
    out["video_url"] = storage.storage.url_for(out.pop("video_key", None))
    job = repo.latest_job(conn, project["id"])
    out["job"] = (
        {"id": job["id"], "status": job["status"], "provider": job["provider"]}
        if job else None
    )
    return out


def _summarize(project: dict) -> dict:
    out = dict(project)
    out["video_url"] = storage.storage.url_for(out.pop("video_key", None))
    return out


# --- health -----------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "reelforge-api",
        "ffmpeg": ffmpeg.ffmpeg_available(),
        "font": bool(ffmpeg.find_font()),
        "db_dialect": config.DB_DIALECT,
        "storage": config.STORAGE_BACKEND,
        "job_runner": config.JOB_RUNNER,
        "video_provider": config.VIDEO_PROVIDER,
        "providers": providers.available_providers(),
    }


# --- storyboard preview -----------------------------------------------------

@app.post("/api/storyboard")
def create_storyboard(request: StoryboardRequest):
    """Stateless storyboard preview. Same shape as the original scaffold."""
    _check_ratio(request.aspect_ratio)
    scenes = storyboard.plan_scenes(request.idea, request.category, request.duration)
    return {
        "duration": request.duration,
        "aspect_ratio": request.aspect_ratio,
        "category": request.category,
        "scenes": scenes,
    }


# --- projects ---------------------------------------------------------------

@app.get("/api/projects")
def list_projects(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    with db.connect() as conn:
        rows = repo.list_projects(conn, limit=limit, offset=offset)
        return {
            "projects": [_summarize(r) for r in rows],
            "total": repo.count_projects(conn),
        }


@app.post("/api/projects", status_code=201)
def create_project(request: ProjectRequest):
    _check_ratio(request.aspect_ratio)
    scenes = storyboard.plan_scenes(request.idea, request.category, request.duration)
    title = (request.title or request.idea).strip()[:120] or "Untitled reel"

    with db.connect() as conn:
        project_id = repo.create_project(
            conn,
            title=title,
            idea=request.idea,
            category=request.category,
            input_type=request.input_type,
            duration=request.duration,
            aspect_ratio=request.aspect_ratio,
        )
        repo.insert_scenes(conn, project_id, scenes)
        # The planner is exact, but keep duration derived from the scenes so
        # there is one source of truth from the moment the project exists.
        repo.sync_project_duration(conn, project_id)
        return _serialize(conn, _require_project(conn, project_id))


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    with db.connect() as conn:
        return _serialize(conn, _require_project(conn, project_id))


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with db.connect() as conn:
        _require_project(conn, project_id)
        repo.delete_project(conn, project_id)
    # Storage is cleaned after the row is gone: an orphaned file is recoverable,
    # a row pointing at a deleted file is not.
    storage.storage.delete_prefix(f"uploads/{project_id}")
    storage.storage.delete_prefix(storage.render_key(project_id))


@app.patch("/api/projects/{project_id}/audio")
def update_audio(project_id: str, request: AudioSettingsRequest):
    if request.music_volume is None and request.music_fade_out is None:
        raise HTTPException(422, "provide music_volume or music_fade_out")
    with db.connect() as conn:
        _require_project(conn, project_id)
        repo.update_audio_settings(
            conn, project_id,
            volume=request.music_volume, fade_out=request.music_fade_out,
        )
        repo.invalidate_render(conn, project_id)
        return _serialize(conn, _require_project(conn, project_id))


# --- scenes -----------------------------------------------------------------

@app.patch("/api/projects/{project_id}/scenes/{scene_id}")
def update_scene(project_id: str, scene_id: str, update: SceneUpdate):
    fields = {k: v for k, v in update.model_dump(exclude_unset=True).items()
              if v is not None}
    if not fields:
        raise HTTPException(422, "no fields to update")

    with db.connect() as conn:
        _require_project(conn, project_id)
        if repo.get_scene(conn, project_id, scene_id) is None:
            raise HTTPException(404, "scene not found")

        # A per-scene cap alone lets 8 scenes reach 240s in a "30-second reel",
        # so validate the resulting total, not just this one scene.
        if "duration" in fields:
            others = repo.total_scene_seconds(conn, project_id, exclude=scene_id)
            total = others + int(fields["duration"])
            if total > config.MAX_REEL_SECONDS:
                raise HTTPException(
                    422,
                    f"that would make the reel {total}s;"
                    f" the maximum is {config.MAX_REEL_SECONDS}s",
                )
            if total < config.MIN_REEL_SECONDS:
                raise HTTPException(
                    422,
                    f"that would make the reel {total}s;"
                    f" the minimum is {config.MIN_REEL_SECONDS}s",
                )

        if not repo.update_scene(conn, project_id, scene_id, fields):
            raise HTTPException(404, "scene not found")
        repo.invalidate_render(conn, project_id)
        repo.sync_project_duration(conn, project_id)
        return _serialize(conn, _require_project(conn, project_id))


@app.post("/api/projects/{project_id}/scenes/{scene_id}/regenerate")
def regenerate_scene(project_id: str, scene_id: str):
    with db.connect() as conn:
        project = _require_project(conn, project_id)
        scene = repo.get_scene(conn, project_id, scene_id)
        if scene is None:
            raise HTTPException(404, "scene not found")

        attempt = int(scene.get("regen_count", 0)) + 1
        prompt = storyboard.reprompt_scene(
            project["idea"], project["category"], scene["title"], attempt
        )
        repo.bump_regeneration(conn, scene_id, prompt)
        repo.invalidate_render(conn, project_id)
        return _serialize(conn, _require_project(conn, project_id))


# --- uploads ----------------------------------------------------------------

@app.post("/api/projects/{project_id}/uploads", status_code=201)
async def upload_asset(
    project_id: str,
    file: UploadFile = File(...),
    scene_id: str | None = None,
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        kind = "image"
    elif suffix in AUDIO_SUFFIXES:
        kind = "audio"
    else:
        raise HTTPException(
            415, f"unsupported file type '{suffix or 'unknown'}';"
                 f" images {sorted(IMAGE_SUFFIXES)} or audio {sorted(AUDIO_SUFFIXES)}"
        )

    # Read in chunks and stop at the limit rather than buffering an arbitrarily
    # large body first and checking its size afterwards.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 256)
        if not chunk:
            break
        total += len(chunk)
        if total > config.MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"file exceeds {config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB"
            )
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise HTTPException(422, "uploaded file is empty")

    with db.connect() as conn:
        _require_project(conn, project_id)
        if scene_id and repo.get_scene(conn, project_id, scene_id) is None:
            raise HTTPException(404, "scene not found")

        asset_id = db.new_id("asset")
        key = storage.upload_key(project_id, asset_id, suffix)
        storage.storage.save_bytes(key, payload)
        repo.insert_asset(
            conn, project_id, asset_id, kind, f"{asset_id}{suffix}", key
        )
        if kind == "image":
            if scene_id:
                repo.attach_asset_to_scene(conn, project_id, scene_id, asset_id)
            else:
                repo.attach_asset_to_bare_scenes(conn, project_id, asset_id)
        repo.invalidate_render(conn, project_id)
        return _serialize(conn, _require_project(conn, project_id))


# --- rendering --------------------------------------------------------------

@app.post("/api/projects/{project_id}/render", status_code=202)
def start_render(project_id: str, request: RenderRequest | None = None):
    if not ffmpeg.ffmpeg_available():
        raise HTTPException(503, "ffmpeg is not installed or not on PATH")

    name = (request.provider if request else None) or config.VIDEO_PROVIDER
    try:
        generator = providers.build_generator(name)
    except ffmpeg.RenderError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not generator.available():
        # Reject here rather than failing halfway through a render.
        raise HTTPException(
            503,
            f"the {generator.name!r} video provider is not configured."
            " Use provider 'mock' to render locally.",
        )

    with db.connect() as conn:
        _require_project(conn, project_id)
        if not repo.claim_for_render(conn, project_id):
            raise HTTPException(409, "this project is already rendering")
        job_id = repo.create_job(conn, project_id, generator.name)

    # Execution happens outside the request. Replacing this with a broker
    # publish is the only change a real queue needs.
    jobs.submit(job_id, project_id)
    return {
        "id": project_id, "job_id": job_id, "status": "rendering",
        "progress": 0, "provider": generator.name,
    }


@app.get("/api/projects/{project_id}/jobs/{job_id}")
def get_job(project_id: str, job_id: str):
    with db.connect() as conn:
        _require_project(conn, project_id)
        job = repo.get_job(conn, job_id)
        if job is None or job["project_id"] != project_id:
            raise HTTPException(404, "job not found")
        return job
