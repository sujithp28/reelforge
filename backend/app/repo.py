"""Every SQL statement in the application.

Routes and the render worker call these functions and never write SQL. That is
what makes the PostgreSQL swap a db.py change: the queries here are already
dialect-neutral (`?` placeholders, `{now}` token) and carry no SQLite-only
syntax.

Each function takes an open connection so callers control the transaction
boundary — an edit that also invalidates a render is one atomic unit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .db import new_id, rows_to_dicts

# Statuses a project can be in. `rendering` is owned by the job worker.
DRAFT, RENDERING, READY, FAILED, CANCELLED = (
    "draft", "rendering", "ready", "failed", "cancelled",
)


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


def set_quality(conn: Any, project_id: str, quality: str) -> None:
    conn.execute(
        db.sql("UPDATE projects SET quality = ?, updated_at = {now} WHERE id = ?"),
        (quality, project_id),
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


def set_clip_ready(
    conn: Any, scene_id: str, *, key: str, clip_hash: str, provider: str
) -> None:
    conn.execute(
        db.sql(
            "UPDATE scenes SET clip_key = ?, clip_hash = ?, clip_status = 'ready',"
            " clip_error = NULL, clip_provider = ? WHERE id = ?"
        ),
        (key, clip_hash, provider, scene_id),
    )


def set_clip_failed(
    conn: Any, scene_id: str, *, clip_hash: str, provider: str, message: str
) -> None:
    """Record a scene-level failure without touching any other scene's clip."""
    conn.execute(
        db.sql(
            "UPDATE scenes SET clip_status = 'failed', clip_error = ?,"
            " clip_hash = ?, clip_provider = ?,"
            " clip_attempts = clip_attempts + 1 WHERE id = ?"
        ),
        (message[:500], clip_hash, provider, scene_id),
    )


def clear_clip(conn: Any, project_id: str, scene_id: str) -> str | None:
    """Forget a scene's cached clip so the next render regenerates just it.

    Returns the storage key that is now unreferenced, for the caller to delete.
    """
    row = conn.execute(
        db.sql("SELECT clip_key FROM scenes WHERE id = ? AND project_id = ?"),
        (scene_id, project_id),
    ).fetchone()
    conn.execute(
        db.sql(
            "UPDATE scenes SET clip_key = NULL, clip_hash = NULL,"
            " clip_status = 'pending', clip_error = NULL WHERE id = ?"
        ),
        (scene_id,),
    )
    return row["clip_key"] if row else None


def clear_all_clips(conn: Any, project_id: str) -> list[str]:
    """Invalidate every cached clip, returning the keys to delete."""
    rows = conn.execute(
        db.sql("SELECT clip_key FROM scenes WHERE project_id = ?"
               " AND clip_key IS NOT NULL"),
        (project_id,),
    ).fetchall()
    conn.execute(
        db.sql(
            "UPDATE scenes SET clip_key = NULL, clip_hash = NULL,"
            " clip_status = 'pending', clip_error = NULL WHERE project_id = ?"
        ),
        (project_id,),
    )
    return [r["clip_key"] for r in rows]


def log_generation(
    conn: Any, *, project_id: str, scene_id: str, job_id: str | None,
    provider: str, attempt: int, status: str, seconds: int | None = None,
    elapsed_ms: int | None = None, error: str | None = None,
) -> None:
    """Audit one generation attempt.

    Deliberately never receives a key, endpoint or raw provider payload — the
    provider redacts before raising, and only its safe message arrives here.
    """
    conn.execute(
        db.sql(
            "INSERT INTO scene_generations (id, project_id, scene_id, job_id,"
            " provider, attempt, status, seconds, elapsed_ms, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,{now})"
        ),
        (new_id("gen"), project_id, scene_id, job_id, provider, attempt, status,
         seconds, elapsed_ms, (error or None) and error[:500]),
    )


def list_generations(conn: Any, project_id: str, limit: int = 100) -> list[dict]:
    return rows_to_dicts(conn.execute(
        db.sql(
            "SELECT * FROM scene_generations WHERE project_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?"
        ),
        (project_id, limit),
    ))


def count_generations(conn: Any, scene_id: str) -> int:
    """Total attempts ever made for one scene, for cost visibility."""
    return int(conn.execute(
        db.sql("SELECT COUNT(*) AS n FROM scene_generations WHERE scene_id = ?"),
        (scene_id,),
    ).fetchone()["n"])


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


def request_cancel(conn: Any, job_id: str) -> bool:
    """Ask a running job to stop. The worker checks this between scenes."""
    changed = conn.execute(
        db.sql(
            "UPDATE render_jobs SET cancel_requested = 1, updated_at = {now}"
            " WHERE id = ? AND status IN (?, ?)"
        ),
        (job_id, "queued", "running"),
    ).rowcount
    return bool(changed)


def cancel_requested(conn: Any, job_id: str) -> bool:
    row = conn.execute(
        db.sql("SELECT cancel_requested FROM render_jobs WHERE id = ?"), (job_id,)
    ).fetchone()
    return bool(row and row["cancel_requested"])


def mark_cancelled(conn: Any, project_id: str, job_id: str) -> None:
    conn.execute(
        db.sql("UPDATE projects SET status = ?, progress = 0,"
               " updated_at = {now} WHERE id = ?"),
        (DRAFT, project_id),
    )
    conn.execute(
        db.sql("UPDATE render_jobs SET status = ?, updated_at = {now} WHERE id = ?"),
        (CANCELLED, job_id),
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


# --- scene jobs (external GPU worker queue) --------------------------------
#
# Statuses: pending -> claimed -> completed | failed | cancelled.
# `pending` is the only claimable state, and claiming is a conditional UPDATE
# so two workers polling at the same moment cannot both take one scene.

JOB_PENDING, JOB_CLAIMED = "pending", "claimed"
JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED = "completed", "failed", "cancelled"
SCENE_JOB_STATUSES = (
    JOB_PENDING, JOB_CLAIMED, JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED,
)
TERMINAL_SCENE_JOB_STATUSES = (JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED)


def create_scene_job(
    conn: Any, *, project_id: str, scene_id: str, provider: str, prompt: str,
    duration: int, width: int, height: int, fps: int, quality: str,
    reference_key: str | None = None,
) -> str:
    job_id = new_id("sjob")
    conn.execute(
        db.sql(
            "INSERT INTO scene_jobs (id, project_id, scene_id, provider, status,"
            " prompt, duration, width, height, fps, quality, reference_key,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,{now})"
        ),
        (job_id, project_id, scene_id, provider, JOB_PENDING, prompt, duration,
         width, height, fps, quality, reference_key),
    )
    return job_id


def get_scene_job(conn: Any, job_id: str) -> dict | None:
    row = conn.execute(
        db.sql("SELECT * FROM scene_jobs WHERE id = ?"), (job_id,)
    ).fetchone()
    return dict(row) if row else None


def claim_next_scene_job(
    conn: Any, *, provider: str, worker_id: str, candidates: int = 20
) -> dict | None:
    """Atomically take the oldest pending job, or return None.

    The conditional UPDATE is the lock: it only matches while the row is still
    `pending`, so of two workers racing for the same row exactly one gets a
    rowcount of 1 and the loser moves to the next candidate. This works the
    same on SQLite and PostgreSQL without SELECT ... FOR UPDATE.
    """
    rows = conn.execute(
        db.sql(
            "SELECT id FROM scene_jobs WHERE status = ? AND provider = ?"
            " ORDER BY created_at, id LIMIT ?"
        ),
        (JOB_PENDING, provider, candidates),
    ).fetchall()
    for row in rows:
        changed = conn.execute(
            db.sql(
                "UPDATE scene_jobs SET status = ?, claimed_by = ?,"
                " claimed_at = {now}, attempts = attempts + 1"
                " WHERE id = ? AND status = ?"
            ),
            (JOB_CLAIMED, worker_id, row["id"], JOB_PENDING),
        ).rowcount
        if changed:
            return get_scene_job(conn, row["id"])
    return None


def complete_scene_job(conn: Any, job_id: str, output_key: str) -> bool:
    """Mark a claimed job done. False if it was not claimed any more."""
    changed = conn.execute(
        db.sql(
            "UPDATE scene_jobs SET status = ?, output_key = ?, error = NULL,"
            " completed_at = {now} WHERE id = ? AND status = ?"
        ),
        (JOB_COMPLETED, output_key, job_id, JOB_CLAIMED),
    ).rowcount
    return bool(changed)


def fail_scene_job(conn: Any, job_id: str, message: str) -> bool:
    """Fail a job that is not already finished."""
    placeholders = ",".join("?" for _ in TERMINAL_SCENE_JOB_STATUSES)
    changed = conn.execute(
        db.sql(
            f"UPDATE scene_jobs SET status = ?, error = ?, completed_at = {{now}}"
            f" WHERE id = ? AND status NOT IN ({placeholders})"
        ),
        (JOB_FAILED, message[:500], job_id, *TERMINAL_SCENE_JOB_STATUSES),
    ).rowcount
    return bool(changed)


def cancel_scene_jobs_for_scene(conn: Any, scene_id: str) -> int:
    placeholders = ",".join("?" for _ in TERMINAL_SCENE_JOB_STATUSES)
    return int(conn.execute(
        db.sql(
            f"UPDATE scene_jobs SET status = ?, completed_at = {{now}}"
            f" WHERE scene_id = ? AND status NOT IN ({placeholders})"
        ),
        (JOB_CANCELLED, scene_id, *TERMINAL_SCENE_JOB_STATUSES),
    ).rowcount)


def requeue_stale_scene_jobs(conn: Any, *, provider: str, claim_timeout_seconds: int,
                             max_attempts: int) -> dict[str, int]:
    """Rescue jobs whose worker disappeared.

    A Kaggle session can be killed at any moment, leaving a job stuck at
    `claimed` with nobody working on it. Anything claimed longer ago than the
    timeout goes back to `pending` so another worker can take it, unless it
    has already used up its attempts, in which case it fails cleanly rather
    than looping on free hardware forever.

    Scoped to one `provider`: different models take wildly different amounts
    of time per clip (an undistilled model can take 30+ minutes where a
    distilled one takes under a minute), so a claim timeout tuned for one
    provider would either wait too long for a truly dead worker on another,
    or — worse — requeue a still-running job out from under it, causing its
    eventual upload to be rejected as stale.
    """
    # The cutoff is computed here rather than in SQL: date arithmetic is one
    # of the few things with no portable spelling, and timestamps are stored
    # in a lexicographically sortable UTC format, so a plain string compare
    # works identically on SQLite and PostgreSQL.
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=int(claim_timeout_seconds))
    ).strftime("%Y-%m-%d %H:%M:%S")
    stale = rows_to_dicts(conn.execute(
        db.sql(
            "SELECT id, attempts FROM scene_jobs WHERE status = ? AND provider = ?"
            " AND claimed_at IS NOT NULL AND claimed_at < ?"
        ),
        (JOB_CLAIMED, provider, cutoff),
    ))
    requeued = failed = 0
    for job in stale:
        if job["attempts"] >= max_attempts:
            conn.execute(
                db.sql(
                    "UPDATE scene_jobs SET status = ?, error = ?,"
                    " completed_at = {now} WHERE id = ? AND status = ?"
                ),
                (JOB_FAILED,
                 "generation was attempted several times without finishing",
                 job["id"], JOB_CLAIMED),
            )
            failed += 1
        else:
            conn.execute(
                db.sql(
                    "UPDATE scene_jobs SET status = ?, claimed_by = NULL,"
                    " claimed_at = NULL WHERE id = ? AND status = ?"
                ),
                (JOB_PENDING, job["id"], JOB_CLAIMED),
            )
            requeued += 1
    return {"requeued": requeued, "failed": failed}


def count_scene_jobs_by_status(conn: Any, provider: str = "kaggle") -> dict[str, int]:
    rows = conn.execute(
        db.sql(
            "SELECT status, COUNT(*) AS n FROM scene_jobs WHERE provider = ?"
            " GROUP BY status"
        ),
        (provider,),
    ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def claim_queued_jobs(conn: Any, limit: int = 10) -> list[dict]:
    """For an external worker process to poll. Unused by the thread runner."""
    return rows_to_dicts(conn.execute(
        db.sql("SELECT * FROM render_jobs WHERE status = ?"
               " ORDER BY created_at LIMIT ?"),
        ("queued", limit),
    ))
