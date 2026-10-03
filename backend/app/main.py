"""HTTP layer.

Routes validate input, call repo.py inside one transaction, and enqueue jobs.
No SQL, no ffmpeg, no filesystem paths live here.
"""
from __future__ import annotations

import hmac
import json
import logging
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    config, db, export_presets, ffmpeg, jobs, kaggle, luxury, providers, repo, storage, storyboard,
)
from .render import CUSTOMER_RENDER_ERROR

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

# Serve only the files a customer is meant to see. The database, work
# directories, and worker staging live in DATA_DIR too, and must not be
# reachable as /media/<name>.
PUBLIC_MEDIA_DIRS = ("uploads", "renders", "clips")
if config.STORAGE_BACKEND == "local":
    for _folder in PUBLIC_MEDIA_DIRS:
        _directory = config.DATA_DIR / _folder
        _directory.mkdir(parents=True, exist_ok=True)
        app.mount(
            f"{config.MEDIA_URL_PREFIX}/{_folder}",
            StaticFiles(directory=_directory),
            name=f"media-{_folder}",
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
    export_preset: str | None = None
    brand_name: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=8)


class StoryboardRequest(BaseModel):
    """Preview-only planner, kept for the original /api/storyboard endpoint."""
    idea: str
    category: str = "Cinematic"
    duration: int = Field(
        default=30, ge=config.MIN_REEL_SECONDS, le=config.MAX_REEL_SECONDS
    )
    aspect_ratio: str = "9:16"


class SceneAssetRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=80)


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class LuxuryScriptRequest(BaseModel):
    topic: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, max_length=8)
    brand_name: str | None = Field(default=None, max_length=80)
    regenerate: bool = False
    instagram_caption: str | None = Field(default=None, max_length=500)
    youtube_description: str | None = Field(default=None, max_length=1200)
    hashtags: list[str] | None = Field(default=None, max_length=12)
    english: dict | None = None
    telugu: dict | None = None
    image_prompts: dict | None = None
    regenerate_image_prompt: int | None = Field(default=None, ge=0, le=4)
    apply_to_scenes: bool = True


class SceneOrderRequest(BaseModel):
    scene_ids: list[str] = Field(min_length=1)


class NewSceneRequest(BaseModel):
    duration: int | None = Field(
        default=None,
        ge=config.MIN_SCENE_SECONDS_ALLOWED,
        le=config.MAX_SCENE_SECONDS_ALLOWED,
    )


class SceneUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=2000)
    caption: str | None = Field(default=None, max_length=200)
    text_title: str | None = Field(default=None, max_length=80)
    text_subtitle: str | None = Field(default=None, max_length=120)
    show_title: bool | None = None
    show_subtitle: bool | None = None
    duration: int | None = Field(
        default=None,
        ge=config.MIN_SCENE_SECONDS_ALLOWED,
        le=config.MAX_SCENE_SECONDS_ALLOWED,
    )


class QualityRequest(BaseModel):
    quality: str


class ExportPresetRequest(BaseModel):
    export_preset: str = Field(min_length=1, max_length=40)


class AudioSettingsRequest(BaseModel):
    music_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    music_fade_out: int | None = Field(default=None, ge=0, le=10)
    music_fade_in: int | None = Field(default=None, ge=0, le=10)
    music_enabled: bool | None = None


class RenderRequest(BaseModel):
    provider: str | None = None


# --- helpers ----------------------------------------------------------------

def _upload_filename(original: str | None, suffix: str) -> str:
    """Keep the customer's file name for the scene picker. The storage key stays an id."""
    raw = Path(original or "").name
    cleaned = "".join(
        ch if ch.isalnum() or ch in " ._-()" else "_" for ch in raw
    ).strip().strip(".")
    if not cleaned:
        cleaned = f"upload{suffix}"
    if suffix and not cleaned.lower().endswith(suffix):
        cleaned = f"{cleaned}{suffix}"
    return cleaned[:120]


async def _read_upload(file: UploadFile) -> bytes:
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
    return payload


def _check_ratio(ratio: str) -> str:
    if ratio not in ALLOWED_RATIOS:
        raise HTTPException(422, f"aspect_ratio must be one of {sorted(ALLOWED_RATIOS)}")
    return ratio


def _check_preset(preset_id: str) -> export_presets.ExportPreset:
    chosen = export_presets.get_preset(preset_id)
    if chosen is None:
        raise HTTPException(422, "Choose a publish format.")
    return chosen


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
        log.warning("provider refused: %s", exc)
        raise HTTPException(503, CUSTOMER_RENDER_ERROR) from exc
    if not generator.available():
        reason = providers.configuration_error(generator.name)
        log.warning("provider %s unavailable: %s", generator.name, reason)
        raise HTTPException(503, CUSTOMER_RENDER_ERROR)
    return generator


def _playable_audio(payload: bytes, suffix: str) -> bool:
    """Reject a renamed text file. The customer only hears whether it can play."""
    handle = tempfile.NamedTemporaryFile(suffix=suffix or ".audio", delete=False)
    tmp_name = handle.name
    try:
        handle.write(payload)
        handle.close()
        return ffmpeg.has_audio_stream(tmp_name)
    finally:
        handle.close()
        Path(tmp_name).unlink(missing_ok=True)


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
        scene["show_title"] = bool(scene.get("show_title"))
        scene["show_subtitle"] = bool(scene.get("show_subtitle"))
        scene["text_title"] = scene.get("text_title") or None
        scene["text_subtitle"] = scene.get("text_subtitle") or None
        # Internal only: the browser never needs the clip cache.
        scene.pop("clip_key", None)
        scene.pop("clip_hash", None)
        scene.pop("clip_provider", None)

    out["scenes"] = scenes
    out["assets"] = assets
    out["music_enabled"] = bool(out.get("music_enabled"))
    out["music_fade_in"] = int(out.get("music_fade_in") if out.get("music_fade_in") is not None else 1)
    out["output_stale"] = bool(out.get("output_stale"))
    raw_script = out.get("content_script")
    if isinstance(raw_script, str) and raw_script.strip():
        try:
            out["content_script"] = json.loads(raw_script)
        except json.JSONDecodeError:
            out["content_script"] = None
    else:
        out["content_script"] = None
    logo = by_id.get(out.get("logo_asset_id"))
    out["logo_url"] = logo["url"] if logo else None
    out["total_duration"] = sum(s["duration"] for s in scenes)
    out["video_url"] = storage.storage.url_for(out.pop("video_key", None))
    job = repo.latest_job(conn, project["id"])
    out["job"] = (
        {"id": job["id"], "status": job["status"]}
        if job else None
    )
    return out


def _summarize(project: dict) -> dict:
    out = dict(project)
    out["video_url"] = storage.storage.url_for(out.pop("video_key", None))
    out["cover_url"] = storage.storage.url_for(out.pop("cover_key", None))
    out.pop("content_script", None)
    out["output_stale"] = bool(out.get("output_stale"))
    return out


def _scene_duration_room(conn, project_id: str, requested: int) -> int:
    others = repo.total_scene_seconds(conn, project_id)
    if others + requested > config.MAX_REEL_SECONDS:
        raise HTTPException(
            422,
            f"that would make the reel {others + requested}s;"
            f" the maximum is {config.MAX_REEL_SECONDS}s",
        )
    return requested


EXPORT_UNAVAILABLE = "Video export is not available on this computer."


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


def _apply_luxury_lines(conn, project_id: str, lines: list[tuple[str, str]]) -> list[str]:
    """Write on-screen titles through the existing scene text fields."""
    stale: list[str] = []
    scenes = repo.list_scenes(conn, project_id)
    for scene, (title, subtitle) in zip(scenes, lines):
        if not title and not subtitle:
            continue
        repo.update_scene(conn, project_id, scene["id"], {
            "text_title": title or None,
            "text_subtitle": subtitle or None,
            "show_title": 1 if title else 0,
            "show_subtitle": 1 if subtitle else 0,
        })
        key = repo.clear_clip(conn, project_id, scene["id"])
        if key:
            stale.append(key)
    current = repo.get_project(conn, project_id)
    if lines and current and current.get("video_key"):
        repo.invalidate_render(conn, project_id)
    return stale


@app.post("/api/projects", status_code=201)
def create_project(request: ProjectRequest):
    preset = _check_preset(request.export_preset) if request.export_preset else None
    if request.category == luxury.CATEGORY and preset is None:
        preset = _check_preset("instagram_reel")
    aspect_ratio = preset.aspect_ratio if preset else _check_ratio(request.aspect_ratio)
    quality = _check_quality(request.quality)
    # Photos are uploaded after the project exists, so the planner has none yet.
    scenes = storyboard.plan(
        request.idea,
        request.category,
        request.duration,
        request.input_type,
        [],
    )
    title = (request.title or request.idea).strip()[:120] or "Untitled reel"

    with db.connect() as conn:
        project_id = repo.create_project(
            conn,
            title=title,
            idea=request.idea,
            category=request.category,
            input_type=request.input_type,
            duration=request.duration,
            aspect_ratio=aspect_ratio,
        )
        if preset is not None:
            repo.set_export_preset(conn, project_id, preset.id, preset.aspect_ratio)
        repo.set_quality(conn, project_id, quality)
        repo.insert_scenes(conn, project_id, scenes)
        if request.category == luxury.CATEGORY:
            script = luxury.build_script(
                request.idea, request.brand_name, request.language,
            )
            repo.set_luxury_copy(
                conn, project_id, script["brand_name"], json.dumps(script),
            )
            _apply_luxury_lines(conn, project_id, luxury.on_screen_lines(script))
        # The planner is exact, but keep duration derived from the scenes so
        # there is one source of truth from the moment the project exists.
        repo.sync_project_duration(conn, project_id)
        return _serialize(conn, _require_project(conn, project_id))


@app.get("/api/luxury/capabilities")
def luxury_capabilities():
    """Whether concept images can be generated. None are, unless a provider is wired."""
    return luxury.image_generation_status()


@app.put("/api/projects/{project_id}/luxury-script")
def update_luxury_script(project_id: str, request: LuxuryScriptRequest):
    """Save the editable luxury copy and, by default, place it on the scenes."""
    stale: list[str] = []
    with db.connect() as conn:
        project = _require_project(conn, project_id)
        if project.get("category") != luxury.CATEGORY:
            raise HTTPException(422, "This script belongs to a Luxury Interiors reel.")
        current = {}
        raw = project.get("content_script")
        if isinstance(raw, str) and raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                current = loaded
        topic = request.topic or current.get("topic") or project.get("idea") or ""
        brand = request.brand_name or current.get("brand_name") or luxury.DEFAULT_BRAND
        language = request.language or current.get("language") or "en"
        if request.regenerate or not current:
            script = luxury.build_script(str(topic), str(brand), str(language))
        else:
            script = luxury.merge_edits(current, request.model_dump(exclude_unset=True))
            if not luxury.image_prompts_ready(script):
                script["image_prompts"] = luxury.build_image_prompts(
                    str(script.get("topic") or topic), script.get("language"),
                )
        if request.regenerate_image_prompt is not None and not request.regenerate:
            script = luxury.replace_image_prompt(script, request.regenerate_image_prompt)
        repo.set_luxury_copy(conn, project_id, script["brand_name"], json.dumps(script))
        if request.apply_to_scenes:
            stale = _apply_luxury_lines(conn, project_id, luxury.on_screen_lines(script))
        payload = _serialize(conn, _require_project(conn, project_id))
    for key in stale:
        storage.storage.delete_prefix(key)
    return payload


@app.post("/api/projects/{project_id}/logo", status_code=201)
async def upload_logo(project_id: str, file: UploadFile = File(...)):
    """Store a brand mark for the studio. It is not composited onto the video."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise HTTPException(415, "Use a JPEG, PNG, WebP, or BMP logo.")
    payload = await _read_upload(file)
    with db.connect() as conn:
        project = _require_project(conn, project_id)
        previous = project.get("logo_asset_id")
        asset_id = db.new_id("asset")
        key = storage.upload_key(project_id, asset_id, suffix)
        storage.storage.save_bytes(key, payload)
        repo.insert_asset(
            conn, project_id, asset_id, "logo",
            _upload_filename(file.filename, suffix), key,
        )
        repo.set_logo_asset(conn, project_id, asset_id)
        removed = None
        if previous and previous != asset_id:
            old = repo.get_asset(conn, project_id, previous)
            if old is not None and old["kind"] == "logo":
                removed = repo.delete_asset(conn, project_id, previous)
        body = _serialize(conn, _require_project(conn, project_id))
    if removed:
        storage.storage.delete_prefix(removed[0])
    return body


@app.patch("/api/projects/{project_id}")
def rename_project(project_id: str, request: RenameRequest):
    title = request.title.strip()
    if not title:
        raise HTTPException(422, "Enter a name for this reel.")
    with db.connect() as conn:
        _require_project(conn, project_id)
        repo.rename_project(conn, project_id, title)
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


@app.patch("/api/projects/{project_id}/export")
def update_export(project_id: str, request: ExportPresetRequest):
    """Customer-facing publish format. Size comes from the preset, not the browser."""
    preset = _check_preset(request.export_preset)
    with db.connect() as conn:
        project = _require_project(conn, project_id)
        previous = export_presets.resolve(
            project.get("export_preset"), project["aspect_ratio"],
        )
        repo.set_export_preset(conn, project_id, preset.id, preset.aspect_ratio)
        stale: list[str] = []
        if (previous.width, previous.height) != (preset.width, preset.height):
            stale = repo.clear_all_clips(conn, project_id)
            repo.invalidate_render(conn, project_id)
    for key in stale:
        storage.storage.delete_prefix(key)
    with db.connect() as conn:
        return _serialize(conn, _require_project(conn, project_id))


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
    if (
        request.music_volume is None
        and request.music_fade_out is None
        and request.music_fade_in is None
        and request.music_enabled is None
    ):
        raise HTTPException(422, "provide a music setting")
    with db.connect() as conn:
        _require_project(conn, project_id)
        repo.update_audio_settings(
            conn, project_id,
            volume=request.music_volume, fade_out=request.music_fade_out,
            fade_in=request.music_fade_in, enabled=request.music_enabled,
        )
        # The mix is applied after the scene clips, so those clips stay.
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


@app.put("/api/projects/{project_id}/scenes/order")
def reorder_scenes(project_id: str, request: SceneOrderRequest):
    with db.connect() as conn:
        _require_project(conn, project_id)
        try:
            stale = repo.reorder_scenes(conn, project_id, request.scene_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        payload = _serialize(conn, _require_project(conn, project_id))
    for key in stale:
        storage.storage.delete_prefix(key)
    return payload


@app.post("/api/projects/{project_id}/scenes", status_code=201)
def add_scene(project_id: str, request: NewSceneRequest | None = None):
    requested = request.duration if request and request.duration is not None else 5
    with db.connect() as conn:
        project = _require_project(conn, project_id)
        duration = _scene_duration_room(conn, project_id, requested)
        count = len(repo.list_scenes(conn, project_id)) + 1
        repo.append_scene(
            conn, project_id,
            title=f"Scene {count}",
            prompt=project["idea"],
            duration=duration,
        )
        return _serialize(conn, _require_project(conn, project_id))


@app.delete("/api/projects/{project_id}/scenes/{scene_id}")
def delete_scene(project_id: str, scene_id: str):
    with db.connect() as conn:
        _require_project(conn, project_id)
        try:
            stale = repo.remove_scene(conn, project_id, scene_id)
        except LookupError:
            raise HTTPException(404, "scene not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        payload = _serialize(conn, _require_project(conn, project_id))
    if stale:
        storage.storage.delete_prefix(stale)
    return payload


@app.delete("/api/projects/{project_id}/assets/{asset_id}")
def delete_asset(project_id: str, asset_id: str):
    with db.connect() as conn:
        _require_project(conn, project_id)
        removed = repo.delete_asset(conn, project_id, asset_id)
        if removed is None:
            raise HTTPException(404, "file not found")
        storage_key, stale = removed
        payload = _serialize(conn, _require_project(conn, project_id))
    storage.storage.delete_prefix(storage_key)
    for key in stale:
        storage.storage.delete_prefix(key)
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


@app.delete("/api/projects/{project_id}/scenes/{scene_id}/asset")
def release_scene_image(project_id: str, scene_id: str):
    with db.connect() as conn:
        _require_project(conn, project_id)
        try:
            storage_key, clip_key = repo.release_scene_image(conn, project_id, scene_id)
        except LookupError:
            raise HTTPException(404, "scene not found") from None
        payload = _serialize(conn, _require_project(conn, project_id))
    if storage_key:
        storage.storage.delete_prefix(storage_key)
    if clip_key:
        storage.storage.delete_prefix(clip_key)
    return payload


@app.put("/api/projects/{project_id}/scenes/{scene_id}/asset")
def assign_scene_asset(
    project_id: str,
    scene_id: str,
    body: SceneAssetRequest,
    replan: bool = Query(False),
):
    """Point one scene at an image already uploaded for this project.

    This does not copy the file. Other scenes keep the asset they already have.
    `replan` is set on the last upload-order link of a batch that still has
    empty scenes, so the planner runs after those links and can replace them.
    """
    stale: list[str] = []
    with db.connect() as conn:
        _require_project(conn, project_id)
        if repo.get_scene(conn, project_id, scene_id) is None:
            raise HTTPException(404, "scene not found")
        asset = repo.get_asset(conn, project_id, body.asset_id)
        if asset is None or asset["kind"] != "image":
            raise HTTPException(404, "image not found")
        repo.attach_asset_to_scene(conn, project_id, scene_id, body.asset_id)
        one = repo.clear_clip(conn, project_id, scene_id)
        if one:
            stale.append(one)
        fitted: list[str] | None = None
        current = _require_project(conn, project_id)
        if replan and current.get("category") != luxury.CATEGORY:
            stale.extend(
                storyboard.replan_uploaded_project(
                    conn, current, storage.storage.url_for,
                )
            )
            # The last link of an upload batch. One scene per image, in order.
            fitted = storyboard.fit_image_timeline(conn, _require_project(conn, project_id))
            if fitted is not None:
                stale.extend(fitted)
        repo.invalidate_render(conn, project_id)
        payload = _serialize(conn, _require_project(conn, project_id))
    for key in stale:
        storage.storage.delete_prefix(key)
    if fitted is not None:
        payload = _auto_render(project_id, payload)
    return payload


# --- uploads ----------------------------------------------------------------

@app.post("/api/projects/{project_id}/uploads", status_code=201)
async def upload_asset(
    project_id: str,
    file: UploadFile = File(...),
    scene_id: str | None = None,
    replan: bool = Query(True),
    append: bool = Query(False),
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        kind = "image"
    elif suffix in AUDIO_SUFFIXES:
        kind = "audio"
    else:
        raise HTTPException(
            415,
            "Use a JPEG, PNG, WebP, or BMP photo, or an MP3, M4A, AAC, WAV, or OGG track.",
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
    if kind == "audio" and not _playable_audio(payload, suffix):
        raise HTTPException(422, "That file could not be used as music.")

    fitted: list[str] | None = None
    luxury_reel = False
    with db.connect() as conn:
        project = _require_project(conn, project_id)
        luxury_reel = project.get("category") == luxury.CATEGORY
        if append and kind != "image":
            raise HTTPException(422, "Only a photo can be added as a new scene.")
        if append and scene_id:
            raise HTTPException(422, "Choose either a scene or a new scene, not both.")
        if scene_id and repo.get_scene(conn, project_id, scene_id) is None:
            raise HTTPException(404, "scene not found")
        if append:
            _scene_duration_room(conn, project_id, 5)

        asset_id = db.new_id("asset")
        key = storage.upload_key(project_id, asset_id, suffix)
        storage.storage.save_bytes(key, payload)
        repo.insert_asset(
            conn, project_id, asset_id, kind, _upload_filename(file.filename, suffix), key
        )
        if kind == "audio":
            repo.update_audio_settings(conn, project_id, enabled=True)
        stale: list[str] = []
        if kind == "image" and append:
            count = len(repo.list_scenes(conn, project_id)) + 1
            repo.append_scene(
                conn, project_id,
                title=f"Scene {count}",
                prompt=project["idea"],
                duration=5,
                asset_id=asset_id,
            )
        elif kind == "image":
            if scene_id:
                repo.attach_asset_to_scene(conn, project_id, scene_id, asset_id)
                one = repo.clear_clip(conn, project_id, scene_id)
                stale = [one] if one else []
            elif luxury_reel:
                # Keep the five scenes. The last photo in a batch assigns
                # library images onto empty scenes, one each.
                stale = []
            else:
                repo.attach_asset_to_bare_scenes(conn, project_id, asset_id)
                stale = repo.clear_all_clips(conn, project_id)
            # A single upload is its own batch. A multi-image batch sends
            # replan=false until the last photo, so the planner runs once
            # after every image in that batch is stored.
            if replan:
                stale.extend(
                    storyboard.replan_uploaded_project(
                        conn, _require_project(conn, project_id), storage.storage.url_for,
                    )
                )
                # replan=false is an in-progress batch. The last photo finishes it.
                fitted = storyboard.fit_image_timeline(
                    conn, _require_project(conn, project_id)
                )
                if fitted is not None:
                    stale.extend(fitted)
        repo.invalidate_render(conn, project_id)
        payload = _serialize(conn, _require_project(conn, project_id))
        if kind == "image":
            attached = sum(1 for scene in payload["scenes"] if scene.get("asset_id") == asset_id)
            log.info(
                "image upload project=%s asset=%s key=%s target=%s attached_scenes=%d",
                project_id, asset_id, key, scene_id or "bare-scenes", attached,
            )
    for key in stale:
        storage.storage.delete_prefix(key)
    if fitted is not None and not luxury_reel:
        payload = _auto_render(project_id, payload)
    return payload


# --- rendering --------------------------------------------------------------

@app.post("/api/projects/{project_id}/render", status_code=202)
def start_render(project_id: str, request: RenderRequest | None = None):
    if not ffmpeg.ffmpeg_available():
        log.error("export unavailable: encoder not found")
        raise HTTPException(503, EXPORT_UNAVAILABLE)

    # The browser cannot choose a provider. A body field is ignored so a
    # customer request cannot target another backend.
    requested = request.provider if request else None
    if requested and requested != config.VIDEO_PROVIDER:
        log.info(
            "ignoring requested provider %r; using server provider %s",
            requested, config.VIDEO_PROVIDER,
        )
    generator = _require_available_provider(config.VIDEO_PROVIDER)

    with db.connect() as conn:
        project = _require_project(conn, project_id)
        _require_renderable(conn, project)
        if not repo.claim_for_render(conn, project_id):
            raise HTTPException(409, "this project is already rendering")
        job_id = repo.create_job(conn, project_id, generator.name)

    # Execution happens outside the request. Replacing this with a broker
    # publish is the only change a real queue needs.
    jobs.submit(job_id, project_id)
    return {
        "id": project_id, "job_id": job_id, "status": "rendering",
        "progress": 0,
    }


def _require_renderable(conn, project: dict) -> None:
    scenes = repo.list_scenes(conn, project["id"])
    if not scenes:
        raise HTTPException(422, "Add at least one scene before rendering.")
    total = sum(int(scene["duration"]) for scene in scenes)
    if total < config.MIN_REEL_SECONDS or total > config.MAX_REEL_SECONDS:
        raise HTTPException(
            422,
            f"The reel is {total}s. It needs to be between"
            f" {config.MIN_REEL_SECONDS}s and {config.MAX_REEL_SECONDS}s.",
        )
    if not (project.get("export_preset") or project.get("aspect_ratio")):
        raise HTTPException(422, "Choose a publish format.")
    if project.get("category") == luxury.CATEGORY:
        missing = luxury.missing_image_message(scenes)
        if missing:
            raise HTTPException(422, missing)


def _auto_render(project_id: str, payload: dict) -> dict:
    """Start the existing render after a finished image batch.

    A missing ffmpeg install or a render that is already running must not
    fail the upload that just stored the photos.
    """
    try:
        start_render(project_id)
    except HTTPException as exc:
        log.info("automatic render did not start for %s: %s", project_id, exc.detail)
        return payload
    with db.connect() as conn:
        return _serialize(conn, _require_project(conn, project_id))


@app.post("/api/projects/{project_id}/scenes/{scene_id}/retry", status_code=202)
def retry_scene(project_id: str, scene_id: str):
    """Regenerate one scene and reassemble, keeping every other scene's clip.

    This is the answer to "Scene 2 isn't right": the other scenes are not
    regenerated, so a retry costs one scene rather than the whole reel.
    """
    if not ffmpeg.ffmpeg_available():
        log.error("export unavailable: encoder not found")
        raise HTTPException(503, EXPORT_UNAVAILABLE)

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
        "status": "rendering", "progress": 0,
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
        # Stored for the worker. Not part of the customer payload.
        job.pop("provider", None)
        return job
