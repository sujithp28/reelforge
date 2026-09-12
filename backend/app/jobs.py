"""Render jobs, separated from the HTTP request layer.

An API request only ever creates a durable `render_jobs` row and hands the id
back. Execution happens outside the request: today a background thread in the
same process, later a real worker.

Swapping in Celery/RQ/arq means replacing `submit()` with a broker publish and
running `run_job()` in the worker. The route code, the job table, and the
project status contract all stay as they are, so the frontend never notices.

Setting REELFORGE_JOB_RUNNER=external makes the API enqueue only, leaving the
rows for a separate process to drain — which is how you verify the split is
real before introducing a broker.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, db, providers, render, repo, storage
from .ffmpeg import RenderError

log = logging.getLogger("reelforge.jobs")

# Guards the thread runner only. A real broker makes this unnecessary.
# ponytail: one worker thread, so renders serialise; move to a broker with
# N workers if throughput matters.
_lock = threading.Lock()
_running: set[str] = set()


def submit(job_id: str, project_id: str) -> None:
    """Hand a queued job to the configured runner."""
    if config.JOB_RUNNER == "external":
        log.info("job %s queued for an external worker", job_id)
        return
    thread = threading.Thread(
        target=run_job, args=(job_id, project_id), name=f"render-{job_id}", daemon=True
    )
    thread.start()


def run_job(job_id: str, project_id: str) -> None:
    """Execute one render job. Safe to call from any worker process."""
    with _lock:
        if job_id in _running:
            return
        _running.add(job_id)
    try:
        _execute(job_id, project_id)
    finally:
        with _lock:
            _running.discard(job_id)


def _progress(job_id: str, project_id: str):
    def report(pct: int) -> None:
        with db.connect() as conn:
            repo.set_progress(conn, project_id, pct)
            repo.set_job_state(conn, job_id, "running", progress=pct)
    return report


def _execute(job_id: str, project_id: str) -> None:
    try:
        with db.connect() as conn:
            project = repo.get_project(conn, project_id)
            if project is None:
                repo.set_job_state(conn, job_id, "failed", error="project was deleted")
                return
            scenes = repo.list_scenes(conn, project_id)
            image_keys = repo.scene_image_keys(conn, project_id)
            music = repo.latest_audio(conn, project_id)
            provider_name = (repo.get_job(conn, job_id) or {}).get("provider")
            repo.set_job_state(conn, job_id, "running", progress=0)

        # Resolve storage keys to local files; a remote backend downloads here.
        images = {
            scene_id: storage.storage.localize(key) if key else None
            for scene_id, key in image_keys.items()
        }
        audio = render.AudioSettings(
            music_path=storage.storage.localize(music["storage_key"]) if music else None,
            volume=float(project.get("music_volume", 0.8) or 0.8),
            fade_out=int(project.get("music_fade_out", 2) or 0),
        )

        generator = providers.build_generator(provider_name)
        work_dir = config.DATA_DIR / "work" / project_id
        # Staged outside the work directory so intermediate cleanup can
        # never touch the finished reel.
        staged = config.DATA_DIR / "work" / f"{project_id}-reel.mp4"

        render.render_reel(
            scenes=scenes,
            images=images,
            aspect_ratio=project["aspect_ratio"],
            work_dir=work_dir,
            out_path=staged,
            generator=generator,
            audio=audio,
            on_progress=_progress(job_id, project_id),
        )

        # The finished file only enters storage once, under a stable key.
        key = storage.render_key(project_id)
        if not staged.exists():
            raise RenderError("the render finished but produced no output file")
        storage.storage.save_file(key, staged)
        render.cleanup(work_dir)
        staged.unlink(missing_ok=True)

        with db.connect() as conn:
            repo.mark_ready(conn, project_id, key)
            repo.set_job_state(conn, job_id, "succeeded", progress=100)
        log.info("job %s rendered project %s", job_id, project_id)

    except (RenderError, OSError, ValueError) as exc:
        _fail(job_id, project_id, str(exc))
    except Exception as exc:  # unexpected: record it, do not lose the project
        log.exception("job %s failed unexpectedly", job_id)
        _fail(job_id, project_id, f"unexpected error: {exc}")


def _fail(job_id: str, project_id: str, message: str) -> None:
    log.warning("job %s failed: %s", job_id, message)
    try:
        with db.connect() as conn:
            repo.mark_failed(conn, project_id, message)
            repo.set_job_state(conn, job_id, "failed", error=message)
    except Exception:  # the DB is the last thing we can report through
        log.exception("could not record failure for job %s", job_id)


def drain_once(limit: int = 10) -> int:
    """Run any queued jobs. Entry point for an external worker process.

        python -c "from app import db, jobs; db.init(); jobs.drain_once()"
    """
    with db.connect() as conn:
        queued = repo.claim_queued_jobs(conn, limit)
    for job in queued:
        run_job(job["id"], job["project_id"])
    return len(queued)
