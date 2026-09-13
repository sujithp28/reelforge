"""HTTP layer.

Routes validate input, call repo.py inside one transaction, and enqueue jobs.
No SQL, no ffmpeg, no filesystem paths live here.
"""
from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    config, db, ffmpeg, jobs, kaggle, providers, repo, storage, storyboard,
)

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
    # Scene jobs outlive the API process, so a restart must requeue anything a
    # dead worker left claimed rather than leaving it stuck. Each Kaggle-hosted
    # provider gets its own timeout: an undistilled model can legitimately hold
    # a claim for much longer than a distilled one, so one shared cutoff would
    # either requeue a still-running job on the slow provider or wait too long
    # to rescue a truly dead one on the fast provider.
    _recovered = {"requeued": 0, "failed": 0}
    for _provider, _timeout in (
        ("kaggle", config.KAGGLE_CLAIM_TIMEOUT_SECONDS),
        ("wan", config.WAN_CLAIM_TIMEOUT_SECONDS),
    ):
        _result = repo.requeue_stale_scene_jobs(
            _conn,
            provider=_provider,
            claim_timeout_seconds=_timeout,
            max_attempts=config.KAGGLE_MAX_ATTEMPTS,
        )
        _recovered["requeued"] += _result["requeued"]
        _recovered["failed"] += _result["failed"]
if _released:
    log.warning("released %d render(s) orphaned by a restart", _released)
if _recovered["requeued"] or _recovered["failed"]:
    log.warning("recovered stale scene jobs: %s", _recovered)

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
    quality: str | None = None


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


class QualityRequest(BaseModel):
    quality: str


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


def _check_quality(quality: str | None) -> str:
    chosen = (quality or config.DEFAULT_QUALITY).lower()
    if chosen not in config.QUALITIES:
        raise HTTPException(
            422, f"quality must be one of {list(config.QUALITIES)}"
        )
    return chosen


def _require_available_provider(name: str):
    """Resolve a provider or refuse the request with a safe message.

    Refusing here rather than mid-render means a misconfiguration never
    leaves a half-generated project behind. The reason is logged for an
    operator; the customer sees only that it is unavailable.
    """
    try:
        generator = providers.build_generator(name)
    except ffmpeg.RenderError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not generator.available():
        reason = providers.configuration_error(generator.name)
        log.warning("provider %s unavailable: %s", generator.name, reason)
        raise HTTPException(
            503,
            f"the {generator.name!r} video provider is not configured."
            " Use provider 'mock' to render locally.",
        )
    return generator


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
        # Internal only: the browser never needs a storage key.
        scene.pop("clip_key", None)

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
    try:
        with db.connect() as conn:
            queue_depth = repo.count_scene_jobs_by_status(conn)
    except Exception:  # health must answer even if the queue cannot be read
        queue_depth = {}
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
        # Whether the worker API is switched on, never the token itself.
        "worker_api_enabled": bool(config.KAGGLE_WORKER_TOKEN),
        "scene_job_queue": queue_depth,
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
    quality = _check_quality(request.quality)
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
        repo.set_quality(conn, project_id, quality)
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
    storage.storage.delete_prefix(f"clips/{project_id}")
    storage.storage.delete_prefix(storage.render_key(project_id))


@app.patch("/api/projects/{project_id}/quality")
def update_quality(project_id: str, request: QualityRequest):
    """Customer-facing quality. Maps to provider settings inside the provider."""
    quality = _check_quality(request.quality)
    with db.connect() as conn:
        _require_project(conn, project_id)
        repo.set_quality(conn, project_id, quality)
        # Quality changes the generated pixels, so cached clips no longer apply.
        stale = repo.clear_all_clips(conn, project_id)
        repo.invalidate_render(conn, project_id)
    for key in stale:
        storage.storage.delete_prefix(key)
    with db.connect() as conn:
        return _serialize(conn, _require_project(conn, project_id))


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
        # Only this scene's generated clip is now stale. Leaving the others
        # cached is what stops one edit from re-billing the whole reel.
        stale = repo.clear_clip(conn, project_id, scene_id)
        repo.invalidate_render(conn, project_id)
        repo.sync_project_duration(conn, project_id)
        payload = _serialize(conn, _require_project(conn, project_id))
    if stale:
        storage.storage.delete_prefix(stale)
    return payload


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
        stale = repo.clear_clip(conn, project_id, scene_id)
        repo.invalidate_render(conn, project_id)
        payload = _serialize(conn, _require_project(conn, project_id))
    if stale:
        storage.storage.delete_prefix(stale)
    return payload


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
        stale: list[str] = []
        if kind == "image":
            if scene_id:
                repo.attach_asset_to_scene(conn, project_id, scene_id, asset_id)
                one = repo.clear_clip(conn, project_id, scene_id)
                stale = [one] if one else []
            else:
                repo.attach_asset_to_bare_scenes(conn, project_id, asset_id)
                stale = repo.clear_all_clips(conn, project_id)
        repo.invalidate_render(conn, project_id)
        payload = _serialize(conn, _require_project(conn, project_id))
    for key in stale:
        storage.storage.delete_prefix(key)
    return payload


# --- rendering --------------------------------------------------------------

@app.post("/api/projects/{project_id}/render", status_code=202)
def start_render(project_id: str, request: RenderRequest | None = None):
    if not ffmpeg.ffmpeg_available():
        raise HTTPException(503, "ffmpeg is not installed or not on PATH")

    name = (request.provider if request else None) or config.VIDEO_PROVIDER
    generator = _require_available_provider(name)

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


@app.post("/api/projects/{project_id}/scenes/{scene_id}/retry", status_code=202)
def retry_scene(project_id: str, scene_id: str):
    """Regenerate one scene and reassemble, keeping every other scene's clip.

    This is the answer to "Scene 2 isn't right": the other scenes are not
    regenerated, so a retry costs one scene rather than the whole reel.
    """
    if not ffmpeg.ffmpeg_available():
        raise HTTPException(503, "ffmpeg is not installed or not on PATH")

    with db.connect() as conn:
        _require_project(conn, project_id)
        if repo.get_scene(conn, project_id, scene_id) is None:
            raise HTTPException(404, "scene not found")
        previous = repo.latest_job(conn, project_id)

    # Reuse the provider this reel was last rendered with. The provider is
    # part of each scene's cache fingerprint, so silently falling back to the
    # server default would invalidate every cached clip and regenerate the
    # whole reel instead of the one scene the customer asked to retry.
    name = (previous or {}).get("provider") or config.VIDEO_PROVIDER
    generator = _require_available_provider(name)

    with db.connect() as conn:
        if not repo.claim_for_render(conn, project_id):
            raise HTTPException(409, "this project is already rendering")
        stale = repo.clear_clip(conn, project_id, scene_id)
        job_id = repo.create_job(conn, project_id, generator.name)
    if stale:
        storage.storage.delete_prefix(stale)

    jobs.submit(job_id, project_id)
    return {
        "id": project_id, "job_id": job_id, "scene_id": scene_id,
        "status": "rendering", "progress": 0, "provider": generator.name,
    }


@app.post("/api/projects/{project_id}/jobs/{job_id}/cancel", status_code=202)
def cancel_job(project_id: str, job_id: str):
    """Ask a running render to stop. The worker checks between scenes."""
    with db.connect() as conn:
        _require_project(conn, project_id)
        job = repo.get_job(conn, job_id)
        if job is None or job["project_id"] != project_id:
            raise HTTPException(404, "job not found")
        if not repo.request_cancel(conn, job_id):
            raise HTTPException(
                409, f"this job is already {job['status']} and cannot be cancelled"
            )
    return {"job_id": job_id, "cancel_requested": True}


@app.get("/api/projects/{project_id}/generations")
def list_generations(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=500),
):
    """Generation audit trail. Operator-facing, carries no provider secrets."""
    with db.connect() as conn:
        _require_project(conn, project_id)
        return {"generations": repo.list_generations(conn, project_id, limit)}


# --- Kaggle worker API ------------------------------------------------------
#
# These routes are for the external GPU worker only, not the frontend. They
# are gated on a shared secret and are the only way the worker can see a job
# or reach a reference image — it never receives a filesystem path.

def _require_worker(authorization: str | None) -> None:
    """Authenticate the GPU worker on a bearer token.

    Compared with hmac.compare_digest so a wrong token cannot be recovered by
    timing the response. Returns 503 rather than 401 when no token is
    configured at all, because that is a server misconfiguration and not the
    caller's fault.
    """
    expected = config.KAGGLE_WORKER_TOKEN
    if not expected:
        raise HTTPException(503, "the worker API is not enabled on this server")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            401, "expected an Authorization: Bearer <token> header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(presented.strip(), expected):
        # Never echo either token, not even a prefix.
        log.warning("worker authentication failed")
        raise HTTPException(401, "invalid worker token")


def _worker_job_view(job: dict) -> dict:
    """Exactly what the worker needs, and nothing else.

    No storage keys, no filesystem paths, no project internals. The reference
    image is offered as an endpoint to call, not a location to read.
    """
    return {
        "job_id": job["id"],
        "scene_id": job["scene_id"],
        "prompt": job["prompt"],
        "seconds": job["duration"],
        "width": job["width"],
        "height": job["height"],
        "fps": job["fps"],
        "quality": job["quality"],
        "attempt": job["attempts"],
        "reference_url": (
            f"/api/worker/kaggle/jobs/{job['id']}/reference"
            if job.get("reference_key") else None
        ),
    }


# Kaggle-hosted GPU workers share one worker API and one auth token; only the
# queue lane they claim from differs, via this allow-list.
WORKER_PROVIDERS = ("kaggle", "wan")


@app.get("/api/worker/kaggle/jobs/next")
def worker_next_job(
    authorization: str | None = Header(default=None),
    worker_id: str = Query(default="kaggle", max_length=64),
    provider: str = Query(default="kaggle"),
):
    """Claim the oldest pending scene job, or report that there is none.

    Claiming is atomic in the repository layer, so two workers polling
    simultaneously cannot be handed the same scene. `provider` selects which
    queue lane to claim from ("kaggle" for LTX-Video, "wan" for Wan 2.1);
    the route path stays "kaggle" for backward compatibility since it names
    the shared worker infrastructure, not any one model.
    """
    if provider not in WORKER_PROVIDERS:
        raise HTTPException(422, f"unknown worker provider {provider!r}")
    _require_worker(authorization)
    claim_timeout = (
        config.WAN_CLAIM_TIMEOUT_SECONDS if provider == "wan"
        else config.KAGGLE_CLAIM_TIMEOUT_SECONDS
    )
    with db.connect() as conn:
        # Rescue anything a dead worker left claimed before handing out work.
        recovered = repo.requeue_stale_scene_jobs(
            conn,
            provider=provider,
            claim_timeout_seconds=claim_timeout,
            max_attempts=config.KAGGLE_MAX_ATTEMPTS,
        )
        if recovered["requeued"] or recovered["failed"]:
            log.info("stale scene jobs: %s", recovered)
        job = repo.claim_next_scene_job(
            conn, provider=provider, worker_id=worker_id[:64]
        )
    if job is None:
        return {"job": None}
    log.info("worker %s claimed scene job %s", worker_id, job["id"])
    return {"job": _worker_job_view(job)}


@app.get("/api/worker/kaggle/jobs/{job_id}/reference")
def worker_job_reference(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    """Stream one job's reference image.

    Addressed by job id, and the storage key comes from that row rather than
    from the caller, so there is no path for a worker to request an arbitrary
    file. `localize` additionally refuses any key that escapes the root.
    """
    _require_worker(authorization)
    with db.connect() as conn:
        job = repo.get_scene_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if not job.get("reference_key"):
        raise HTTPException(404, "this job has no reference image")
    path = storage.storage.localize(job["reference_key"])
    if path is None:
        raise HTTPException(404, "the reference image is no longer available")
    return FileResponse(path)


@app.post("/api/worker/kaggle/jobs/{job_id}/complete")
async def worker_complete_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    file: UploadFile = File(...),
):
    """Accept a generated clip for a claimed job.

    The upload is validated before it is accepted: size, declared type, and
    an ffprobe check that it really is decodable video. A worker cannot choose
    where the file lands — the key is derived from the job id.
    """
    _require_worker(authorization)
    with db.connect() as conn:
        job = repo.get_scene_job(conn, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] != repo.JOB_CLAIMED:
        raise HTTPException(
            409, f"this job is {job['status']} and is not awaiting an upload"
        )

    declared = (file.content_type or "").lower()
    if declared and not (declared.startswith("video/")
                         or declared == "application/octet-stream"):
        raise HTTPException(415, f"unsupported content type '{declared}'")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 512)
        if not chunk:
            break
        total += len(chunk)
        if total > config.KAGGLE_MAX_CLIP_BYTES:
            raise HTTPException(
                413,
                f"clip exceeds {config.KAGGLE_MAX_CLIP_BYTES // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    if not chunks:
        raise HTTPException(422, "the uploaded clip was empty")

    key = storage.kaggle_staging_key(job_id)
    storage.storage.save_bytes(key, b"".join(chunks))

    # Trust nothing: confirm ffmpeg can actually read it before we accept it.
    staged = storage.storage.localize(key)
    probed = ffmpeg.probe_duration(str(staged)) if staged else None
    if staged is None or probed is None:
        storage.storage.delete_prefix(key)
        raise HTTPException(422, "the uploaded file is not readable video")

    # A clip shorter than the scene cannot be trimmed up to length. Accepting
    # one would silently produce a scene shorter than the storyboard says,
    # which is the exact corruption normalisation exists to prevent, so it is
    # refused here and the job goes back for another attempt.
    if probed + kaggle.DURATION_TOLERANCE_SECONDS < job["duration"]:
        storage.storage.delete_prefix(key)
        log.warning(
            "scene job %s upload is %.2fs but the scene needs %ss",
            job_id, probed, job["duration"],
        )
        raise HTTPException(
            422,
            f"the clip is {probed:.1f}s but this scene needs"
            f" {job['duration']}s; generate at least the requested length",
        )

    with db.connect() as conn:
        if not repo.complete_scene_job(conn, job_id, key):
            raise HTTPException(409, "this job is no longer awaiting an upload")
    log.info("scene job %s completed by worker (%d bytes)", job_id, total)
    return {"job_id": job_id, "status": repo.JOB_COMPLETED}


class WorkerFailureRequest(BaseModel):
    # Free-text, worker-supplied, and therefore never shown to a customer.
    detail: str | None = Field(default=None, max_length=2000)
    retryable: bool = True


@app.post("/api/worker/kaggle/jobs/{job_id}/fail")
def worker_fail_job(
    job_id: str,
    request: WorkerFailureRequest,
    authorization: str | None = Header(default=None),
):
    """Report that generation failed.

    The worker's detail is logged for an operator but deliberately not stored
    as the customer-facing reason: it can contain model names, CUDA errors and
    stack traces. A retryable failure goes back into the queue so another
    worker, or the same one after a restart, can pick it up.
    """
    _require_worker(authorization)
    with db.connect() as conn:
        job = repo.get_scene_job(conn, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job["status"] in repo.TERMINAL_SCENE_JOB_STATUSES:
            raise HTTPException(409, f"this job is already {job['status']}")

        log.warning(
            "worker reported failure for scene job %s (attempt %d): %s",
            job_id, job["attempts"],
            kaggle.redact_token((request.detail or "no detail")[:500]),
        )
        if request.retryable and job["attempts"] < config.KAGGLE_MAX_ATTEMPTS:
            conn.execute(
                db.sql(
                    "UPDATE scene_jobs SET status = ?, claimed_by = NULL,"
                    " claimed_at = NULL WHERE id = ?"
                ),
                (repo.JOB_PENDING, job_id),
            )
            return {"job_id": job_id, "status": repo.JOB_PENDING}
        repo.fail_scene_job(conn, job_id, kaggle.FAILED_MESSAGE)
    return {"job_id": job_id, "status": repo.JOB_FAILED}


@app.get("/api/projects/{project_id}/jobs/{job_id}")
def get_job(project_id: str, job_id: str):
    with db.connect() as conn:
        _require_project(conn, project_id)
        job = repo.get_job(conn, job_id)
        if job is None or job["project_id"] != project_id:
            raise HTTPException(404, "job not found")
        return job
