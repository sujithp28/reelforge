"""Every SQL statement in the application.

Routes and the render worker call these functions and never write SQL. That is
what makes the PostgreSQL swap a db.py change: the queries here are already
dialect-neutral (`?` placeholders, `{now}` token) and carry no SQLite-only
syntax.

Each function takes an open connection so callers control the transaction
boundary — an edit that also invalidates a render is one atomic unit.
"""
from __future__ import annotations

from typing import Any

from . import db
from .db import new_id, rows_to_dicts

# Statuses a project can be in. `rendering` is owned by the job worker.
DRAFT, RENDERING, READY, FAILED = "draft", "rendering", "ready", "failed"


# --- projects ---------------------------------------------------------------

def create_project(
    conn: Any,
    *,
    title: str,
    idea: str,
    category: str,
    input_type: str,
    duration: int,
    aspect_ratio: str,
) -> str:
    project_id = new_id("proj")
    conn.execute(
        db.sql(
            "INSERT INTO projects (id, title, idea, category, input_type, duration,"
            " aspect_ratio, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,{now},{now})"
        ),
        (project_id, title, idea, category, input_type, duration, aspect_ratio),
    )
    return project_id


def get_project(conn: Any, project_id: str) -> dict | None:
    row = conn.execute(
        db.sql("SELECT * FROM projects WHERE id = ?"), (project_id,)
    ).fetchone()
    return dict(row) if row else None


def list_projects(conn: Any, limit: int = 100, offset: int = 0) -> list[dict]:
    return rows_to_dicts(conn.execute(
        db.sql(
            "SELECT p.*, (SELECT COUNT(*) FROM scenes s WHERE s.project_id = p.id)"
            " AS scene_count FROM projects p"
            " ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?"
        ),
        (limit, offset),
    ))


def count_projects(conn: Any) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]


def delete_project(conn: Any, project_id: str) -> None:
    conn.execute(db.sql("DELETE FROM projects WHERE id = ?"), (project_id,))


def update_audio_settings(
    conn: Any, project_id: str, *, volume: float | None, fade_out: int | None
) -> None:
    sets, params = [], []
    if volume is not None:
        sets.append("music_volume = ?")
        params.append(volume)
    if fade_out is not None:
        sets.append("music_fade_out = ?")
        params.append(fade_out)
    if not sets:
        return
    params.append(project_id)
    conn.execute(
        db.sql(f"UPDATE projects SET {', '.join(sets)}, updated_at = {{now}}"
               " WHERE id = ?"),
        tuple(params),
    )


def invalidate_render(conn: Any, project_id: str) -> None:
    """Drop a rendered video that no longer matches the storyboard.

    Called after any storyboard or asset change so the preview can never show
    a reel that does not match what is on screen.
    """
    conn.execute(
        db.sql(
            "UPDATE projects SET status = ?, progress = 0, error = NULL,"
            " video_key = NULL, updated_at = {now} WHERE id = ?"
        ),
        (DRAFT, project_id),
    )


def sync_project_duration(conn: Any, project_id: str) -> int:
    """Make `duration` the sum of the scene durations, and return it.

    Without this, `project.duration` keeps the originally requested length
    while scene edits change the real total, so the dashboard and the project
    page disagree about how long the same reel is.
    """
    total = conn.execute(
        db.sql("SELECT COALESCE(SUM(duration), 0) AS total FROM scenes"
               " WHERE project_id = ?"),
        (project_id,),
    ).fetchone()["total"]
    conn.execute(
        db.sql("UPDATE projects SET duration = ?, updated_at = {now} WHERE id = ?"),
        (total, project_id),
    )
    return int(total)


def mark_ready(conn: Any, project_id: str, video_key: str) -> None:
    conn.execute(
        db.sql(
            "UPDATE projects SET status = ?, progress = 100, error = NULL,"
            " video_key = ?, updated_at = {now} WHERE id = ?"
        ),
        (READY, video_key, project_id),
    )


def mark_failed(conn: Any, project_id: str, message: str) -> None:
    conn.execute(
        db.sql(
            "UPDATE projects SET status = ?, error = ?, updated_at = {now}"
            " WHERE id = ?"
        ),
        (FAILED, message[:500], project_id),
    )


def set_progress(conn: Any, project_id: str, progress: int) -> None:
    conn.execute(
        db.sql("UPDATE projects SET progress = ?, updated_at = {now} WHERE id = ?"),
        (progress, project_id),
    )


def claim_for_render(conn: Any, project_id: str) -> bool:
    """Atomically move a project into `rendering`. False if already rendering.

    A conditional UPDATE rather than check-then-set: two concurrent render
    requests would both pass a separate SELECT and start two ffmpeg pipelines
    writing the same output file.
    """
    changed = conn.execute(
        db.sql(
            "UPDATE projects SET status = ?, progress = 0, error = NULL,"
            " updated_at = {now} WHERE id = ? AND status <> ?"
        ),
        (RENDERING, project_id, RENDERING),
    ).rowcount
    return bool(changed)


def release_stale_renders(conn: Any) -> int:
    """Fail any project left `rendering` by a process that died.

    In-process jobs do not survive a restart, so without this a killed server
    leaves a project spinning at 'rendering' forever with no way back.
    """
    changed = conn.execute(
        db.sql(
            "UPDATE projects SET status = ?, error = ?, updated_at = {now}"
            " WHERE status = ?"
        ),
        (FAILED, "the server restarted while this reel was rendering", RENDERING),
    ).rowcount
    conn.execute(
        db.sql("UPDATE render_jobs SET status = ?, error = ?, updated_at = {now}"
               " WHERE status IN (?, ?)"),
        ("failed", "the server restarted while this job was running",
         "queued", "running"),
    )
    return int(changed)


# --- scenes -----------------------------------------------------------------

def insert_scenes(conn: Any, project_id: str, scenes: list[dict]) -> None:
    conn.executemany(
        db.sql(
            "INSERT INTO scenes (id, project_id, position, title, prompt, duration,"
            " caption) VALUES (?,?,?,?,?,?,?)"
        ),
        [
            (new_id("scene"), project_id, s["position"], s["title"], s["prompt"],
             s["duration"], s["caption"])
            for s in scenes
        ],
    )


def list_scenes(conn: Any, project_id: str) -> list[dict]:
    return rows_to_dicts(conn.execute(
        db.sql("SELECT * FROM scenes WHERE project_id = ? ORDER BY position"),
        (project_id,),
    ))


def get_scene(conn: Any, project_id: str, scene_id: str) -> dict | None:
    row = conn.execute(
        db.sql("SELECT * FROM scenes WHERE id = ? AND project_id = ?"),
        (scene_id, project_id),
    ).fetchone()
    return dict(row) if row else None


def update_scene(conn: Any, project_id: str, scene_id: str, fields: dict) -> bool:
    if not fields:
        return False
    allowed = {"title", "prompt", "caption", "duration"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update scene columns: {sorted(unknown)}")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    changed = conn.execute(
        db.sql(f"UPDATE scenes SET {assignments} WHERE id = ? AND project_id = ?"),
        (*fields.values(), scene_id, project_id),
    ).rowcount
    return bool(changed)


def bump_regeneration(conn: Any, scene_id: str, prompt: str) -> int:
    """Store a regenerated prompt and return the new attempt number.

    The attempt count is a column rather than something inferred from the
    prompt text, so a user typing an em-dash cannot change the variation
    sequence.
    """
    conn.execute(
        db.sql("UPDATE scenes SET prompt = ?, regen_count = regen_count + 1"
               " WHERE id = ?"),
        (prompt, scene_id),
    )
    return int(conn.execute(
        db.sql("SELECT regen_count FROM scenes WHERE id = ?"), (scene_id,)
    ).fetchone()["regen_count"])


def total_scene_seconds(conn: Any, project_id: str, exclude: str | None = None) -> int:
    query = "SELECT COALESCE(SUM(duration), 0) AS total FROM scenes WHERE project_id = ?"
    params: tuple = (project_id,)
    if exclude:
        query += " AND id <> ?"
        params = (project_id, exclude)
    return int(conn.execute(db.sql(query), params).fetchone()["total"])


# --- assets -----------------------------------------------------------------

def insert_asset(
    conn: Any, project_id: str, asset_id: str, kind: str, filename: str, key: str
) -> None:
    conn.execute(
        db.sql(
            "INSERT INTO assets (id, project_id, kind, filename, storage_key,"
            " created_at) VALUES (?,?,?,?,?,{now})"
        ),
        (asset_id, project_id, kind, filename, key),
    )


def list_assets(conn: Any, project_id: str) -> list[dict]:
    return rows_to_dicts(conn.execute(
        db.sql(
            "SELECT id, kind, filename, storage_key FROM assets"
            " WHERE project_id = ? ORDER BY created_at, id"
        ),
        (project_id,),
    ))


def latest_audio(conn: Any, project_id: str) -> dict | None:
    row = conn.execute(
        db.sql(
            "SELECT * FROM assets WHERE project_id = ? AND kind = 'audio'"
            " ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


def attach_asset_to_scene(conn: Any, project_id: str, scene_id: str, asset_id: str) -> None:
    conn.execute(
        db.sql("UPDATE scenes SET asset_id = ? WHERE id = ? AND project_id = ?"),
        (asset_id, scene_id, project_id),
    )


def attach_asset_to_bare_scenes(conn: Any, project_id: str, asset_id: str) -> None:
    """Seed every scene that has no still yet."""
    conn.execute(
        db.sql("UPDATE scenes SET asset_id = ? WHERE project_id = ?"
               " AND asset_id IS NULL"),
        (asset_id, project_id),
    )


def scene_image_keys(conn: Any, project_id: str) -> dict[str, str | None]:
    rows = conn.execute(
        db.sql(
            "SELECT s.id AS scene_id, a.storage_key AS storage_key FROM scenes s"
            " LEFT JOIN assets a ON a.id = s.asset_id"
            " WHERE s.project_id = ? ORDER BY s.position"
        ),
        (project_id,),
    ).fetchall()
    return {r["scene_id"]: r["storage_key"] for r in rows}


# --- render jobs ------------------------------------------------------------

def create_job(conn: Any, project_id: str, provider: str) -> str:
    job_id = new_id("job")
    conn.execute(
        db.sql(
            "INSERT INTO render_jobs (id, project_id, status, provider,"
            " created_at, updated_at) VALUES (?,?,?,?,{now},{now})"
        ),
        (job_id, project_id, "queued", provider),
    )
    return job_id


def set_job_state(
    conn: Any, job_id: str, status: str, *, progress: int | None = None,
    error: str | None = None,
) -> None:
    sets = ["status = ?"]
    params: list[Any] = [status]
    if progress is not None:
        sets.append("progress = ?")
        params.append(progress)
    if error is not None:
        sets.append("error = ?")
        params.append(error[:500])
    params.append(job_id)
    conn.execute(
        db.sql(f"UPDATE render_jobs SET {', '.join(sets)}, updated_at = {{now}}"
               " WHERE id = ?"),
        tuple(params),
    )


def get_job(conn: Any, job_id: str) -> dict | None:
    row = conn.execute(
        db.sql("SELECT * FROM render_jobs WHERE id = ?"), (job_id,)
    ).fetchone()
    return dict(row) if row else None


def latest_job(conn: Any, project_id: str) -> dict | None:
    row = conn.execute(
        db.sql(
            "SELECT * FROM render_jobs WHERE project_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


def claim_queued_jobs(conn: Any, limit: int = 10) -> list[dict]:
    """For an external worker process to poll. Unused by the thread runner."""
    return rows_to_dicts(conn.execute(
        db.sql("SELECT * FROM render_jobs WHERE status = ?"
               " ORDER BY created_at LIMIT ?"),
        ("queued", limit),
    ))
