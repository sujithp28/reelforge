from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, render, storyboard

app = FastAPI(title="ReelForge API", version="1.0.0")

# The Next.js dev server runs on a different origin, so without this every
# browser request fails preflight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init()
# Uploaded stills and finished reels are served straight off disk.
app.mount("/media/uploads", StaticFiles(directory=db.UPLOAD_DIR), name="uploads")
app.mount("/media/renders", StaticFiles(directory=db.RENDER_DIR), name="renders")

ALLOWED_RATIOS = set(storyboard.ASPECT_SIZES)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".ogg"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ProjectRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=2000)
    category: str = "Cinematic"
    input_type: str = "Idea"
    duration: int = Field(default=30, ge=5, le=120)
    aspect_ratio: str = "9:16"
    title: str | None = Field(default=None, max_length=120)


class StoryboardRequest(BaseModel):
    """Preview-only planner, kept for the original /api/storyboard endpoint."""
    idea: str
    category: str = "Cinematic"
    duration: int = Field(default=30, ge=5, le=120)
    aspect_ratio: str = "9:16"


class SceneUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    prompt: str | None = Field(default=None, max_length=2000)
    caption: str | None = Field(default=None, max_length=200)
    duration: int | None = Field(default=None, ge=1, le=30)


def _check_ratio(ratio: str) -> str:
    if ratio not in ALLOWED_RATIOS:
        raise HTTPException(422, f"aspect_ratio must be one of {sorted(ALLOWED_RATIOS)}")
    return ratio


def _project_row(conn, project_id: str):
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "project not found")
    return row


def _serialize(conn, row) -> dict:
    project = dict(row)
    scenes = db.rows_to_dicts(conn.execute(
        "SELECT * FROM scenes WHERE project_id = ? ORDER BY position", (row["id"],)
    ))
    assets = db.rows_to_dicts(conn.execute(
        "SELECT id, kind, filename FROM assets WHERE project_id = ? ORDER BY created_at",
        (row["id"],),
    ))
    for asset in assets:
        asset["url"] = f"/media/uploads/{project['id']}/{asset['filename']}"
    by_id = {a["id"]: a for a in assets}
    for scene in scenes:
        asset = by_id.get(scene.get("asset_id"))
        scene["asset_url"] = asset["url"] if asset else None
    project["scenes"] = scenes
    project["assets"] = assets
    project["total_duration"] = sum(s["duration"] for s in scenes)
    video = project.pop("video_path", None)
    project["video_url"] = f"/media/renders/{Path(video).name}" if video else None
    return project


@app.get("/health")
def health():
    return {"status": "ok", "service": "reelforge-api", "ffmpeg": render.ffmpeg_available()}


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


@app.get("/api/projects")
def list_projects():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM scenes s WHERE s.project_id = p.id)"
            " AS scene_count FROM projects p ORDER BY p.created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            video = item.pop("video_path", None)
            item["video_url"] = f"/media/renders/{Path(video).name}" if video else None
            out.append(item)
        return {"projects": out}


@app.post("/api/projects", status_code=201)
def create_project(request: ProjectRequest):
    _check_ratio(request.aspect_ratio)
    project_id = db.new_id("proj")
    scenes = storyboard.plan_scenes(request.idea, request.category, request.duration)
    title = (request.title or request.idea).strip()[:120] or "Untitled reel"

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, idea, category, input_type, duration,"
            " aspect_ratio) VALUES (?,?,?,?,?,?,?)",
            (project_id, title, request.idea, request.category, request.input_type,
             request.duration, request.aspect_ratio),
        )
        conn.executemany(
            "INSERT INTO scenes (id, project_id, position, title, prompt, duration,"
            " caption) VALUES (?,?,?,?,?,?,?)",
            [(db.new_id("scene"), project_id, s["position"], s["title"], s["prompt"],
              s["duration"], s["caption"]) for s in scenes],
        )
        return _serialize(conn, _project_row(conn, project_id))


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    with db.connect() as conn:
        return _serialize(conn, _project_row(conn, project_id))


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with db.connect() as conn:
        _project_row(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    for path in (db.UPLOAD_DIR / project_id, db.RENDER_DIR / project_id):
        if path.exists():
            for child in path.rglob("*"):
                child.unlink(missing_ok=True)
            path.rmdir()
    (db.RENDER_DIR / f"{project_id}.mp4").unlink(missing_ok=True)


@app.patch("/api/projects/{project_id}/scenes/{scene_id}")
def update_scene(project_id: str, scene_id: str, update: SceneUpdate):
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(422, "no fields to update")
    with db.connect() as conn:
        _project_row(conn, project_id)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        changed = conn.execute(
            f"UPDATE scenes SET {assignments} WHERE id = ? AND project_id = ?",
            (*fields.values(), scene_id, project_id),
        ).rowcount
        if not changed:
            raise HTTPException(404, "scene not found")
        # Editing a scene invalidates the rendered video.
        conn.execute(
            "UPDATE projects SET status = 'draft', progress = 0, video_path = NULL,"
            " updated_at = datetime('now') WHERE id = ?", (project_id,),
        )
        return _serialize(conn, _project_row(conn, project_id))


@app.post("/api/projects/{project_id}/scenes/{scene_id}/regenerate")
def regenerate_scene(project_id: str, scene_id: str):
    with db.connect() as conn:
        project = _project_row(conn, project_id)
        scene = conn.execute(
            "SELECT * FROM scenes WHERE id = ? AND project_id = ?", (scene_id, project_id)
        ).fetchone()
        if scene is None:
            raise HTTPException(404, "scene not found")
        # Cycle a style variation each time the user asks again.
        attempt = len(scene["prompt"].split(" — "))
        prompt = storyboard.reprompt_scene(
            project["idea"], project["category"], scene["title"], attempt
        )
        conn.execute("UPDATE scenes SET prompt = ? WHERE id = ?", (prompt, scene_id))
        conn.execute(
            "UPDATE projects SET status = 'draft', progress = 0, video_path = NULL,"
            " updated_at = datetime('now') WHERE id = ?", (project_id,),
        )
        return _serialize(conn, _project_row(conn, project_id))


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

    payload = await file.read()
    if not payload:
        raise HTTPException(422, "uploaded file is empty")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")

    with db.connect() as conn:
        _project_row(conn, project_id)
        asset_id = db.new_id("asset")
        filename = f"{asset_id}{suffix}"
        folder = db.UPLOAD_DIR / project_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_bytes(payload)
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, filename, stored_path)"
            " VALUES (?,?,?,?,?)",
            (asset_id, project_id, kind, filename, str(folder / filename)),
        )
        if kind == "image":
            if scene_id:
                conn.execute(
                    "UPDATE scenes SET asset_id = ? WHERE id = ? AND project_id = ?",
                    (asset_id, scene_id, project_id),
                )
            else:
                # No scene named: seed every scene that has no still yet.
                conn.execute(
                    "UPDATE scenes SET asset_id = ? WHERE project_id = ?"
                    " AND asset_id IS NULL", (asset_id, project_id),
                )
        conn.execute(
            "UPDATE projects SET status = 'draft', progress = 0, video_path = NULL,"
            " updated_at = datetime('now') WHERE id = ?", (project_id,),
        )
        return _serialize(conn, _project_row(conn, project_id))


def _do_render(project_id: str) -> None:
    """Runs on a background task. Owns its own connection."""
    def progress(pct: int) -> None:
        with db.connect() as conn:
            conn.execute(
                "UPDATE projects SET progress = ?, updated_at = datetime('now')"
                " WHERE id = ?", (pct, project_id),
            )

    try:
        with db.connect() as conn:
            project = dict(_project_row(conn, project_id))
            scenes = db.rows_to_dicts(conn.execute(
                "SELECT * FROM scenes WHERE project_id = ? ORDER BY position",
                (project_id,),
            ))
            images = {}
            for scene in scenes:
                if scene["asset_id"]:
                    row = conn.execute(
                        "SELECT stored_path FROM assets WHERE id = ?", (scene["asset_id"],)
                    ).fetchone()
                    images[scene["id"]] = row["stored_path"] if row else None
            music_row = conn.execute(
                "SELECT stored_path FROM assets WHERE project_id = ? AND kind = 'audio'"
                " ORDER BY created_at DESC LIMIT 1", (project_id,),
            ).fetchone()

        out_path = db.RENDER_DIR / f"{project_id}.mp4"
        render.render_reel(
            scenes=scenes,
            images=images,
            aspect_ratio=project["aspect_ratio"],
            work_dir=db.RENDER_DIR / f"{project_id}_work",
            out_path=out_path,
            music=music_row["stored_path"] if music_row else None,
            on_progress=progress,
        )
        with db.connect() as conn:
            conn.execute(
                "UPDATE projects SET status = 'ready', progress = 100, error = NULL,"
                " video_path = ?, updated_at = datetime('now') WHERE id = ?",
                (str(out_path), project_id),
            )
    except Exception as exc:  # surfaced to the user via project.error
        with db.connect() as conn:
            conn.execute(
                "UPDATE projects SET status = 'failed', error = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (str(exc)[:500], project_id),
            )
    finally:
        work = db.RENDER_DIR / f"{project_id}_work"
        if work.exists():
            for child in work.rglob("*"):
                child.unlink(missing_ok=True)
            work.rmdir()


@app.post("/api/projects/{project_id}/render", status_code=202)
def start_render(project_id: str, background: BackgroundTasks):
    if not render.ffmpeg_available():
        raise HTTPException(503, "ffmpeg is not installed or not on PATH")
    with db.connect() as conn:
        project = _project_row(conn, project_id)
        if project["status"] == "rendering":
            raise HTTPException(409, "this project is already rendering")
        conn.execute(
            "UPDATE projects SET status = 'rendering', progress = 0, error = NULL,"
            " updated_at = datetime('now') WHERE id = ?", (project_id,),
        )
    background.add_task(_do_render, project_id)
    return {"id": project_id, "status": "rendering", "progress": 0}
