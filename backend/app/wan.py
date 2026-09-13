"""Wan 2.1 GPU beta provider.

Same shape as kaggle.py, and deliberately so: it reuses the exact queue and
worker-API mechanism already proven there. render.py, jobs.py and the
frontend know only that a provider named "wan" exists.

HOW IT WORKS
------------
Identical inversion of control to the "kaggle" provider: the backend never
starts or contacts a Kaggle notebook. A worker running there polls the same
worker API for jobs whose `provider` column is "wan" instead of "kaggle".

    render.py  ->  WanGenerator.generate()
                       enqueues a scene_jobs row, then waits
    Wan worker ->  GET  /api/worker/kaggle/jobs/next?provider=wan
                   GET  /api/worker/kaggle/jobs/{id}/reference
                   POST /api/worker/kaggle/jobs/{id}/complete
    WanGenerator   sees `completed`, normalises the upload with ffmpeg,
                   writes it to the output path render.py asked for

Why a separate provider rather than reusing "kaggle": they are different
models with different capabilities, timeouts and failure modes, even though
the underlying infrastructure (a Kaggle T4 session polling in) is the same.
The worker-API routes and auth token are shared on purpose; only the queue
lane differs.

WHAT IS NOT TRUSTED
-------------------
Same contract as kaggle.py: the uploaded clip's duration, resolution and
frame rate are all treated as suggestions and normalised by ffmpeg before
they enter the render pipeline.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import config, db, ffmpeg, repo, storage
from .ffmpeg import RenderError
from .kaggle import DURATION_TOLERANCE_SECONDS, FAILED_MESSAGE, redact_token
from .providers import SceneSpec

log = logging.getLogger("reelforge.wan")

UNAVAILABLE_MESSAGE = (
    "AI generation is temporarily unavailable. Please try this scene again."
)


class WanGenerator:
    """Queues a scene for an external Wan 2.1 worker and waits for the result."""

    name = "wan"

    def __init__(
        self,
        worker_token: str = "",
        job_timeout: int = 2400,
        poll_seconds: float = 3.0,
    ):
        self.worker_token = worker_token
        self.job_timeout = job_timeout
        self.poll_seconds = max(0.05, poll_seconds)

    # -- availability --------------------------------------------------------

    def available(self) -> bool:
        return bool(self.worker_token)

    def configuration_error(self) -> str | None:
        if not self.worker_token:
            return "REELFORGE_KAGGLE_WORKER_TOKEN is not set"
        return None

    # -- generation ----------------------------------------------------------

    def _enqueue(self, spec: SceneSpec) -> str:
        with db.connect() as conn:
            row = conn.execute(
                db.sql("SELECT project_id, asset_id FROM scenes WHERE id = ?"),
                (spec.scene_id,),
            ).fetchone()
            if row is None:
                raise RenderError("this scene no longer exists")

            reference_key = None
            if row["asset_id"]:
                asset = conn.execute(
                    db.sql("SELECT storage_key FROM assets WHERE id = ?"),
                    (row["asset_id"],),
                ).fetchone()
                reference_key = asset["storage_key"] if asset else None

            return repo.create_scene_job(
                conn,
                project_id=row["project_id"],
                scene_id=spec.scene_id,
                provider=self.name,
                prompt=spec.prompt,
                duration=int(spec.seconds),
                width=spec.width,
                height=spec.height,
                fps=spec.fps,
                quality=spec.quality,
                reference_key=reference_key,
            )

    def _wait_for(self, job_id: str) -> dict:
        deadline = time.monotonic() + self.job_timeout
        while True:
            with db.connect() as conn:
                repo.requeue_stale_scene_jobs(
                    conn,
                    claim_timeout_seconds=config.KAGGLE_CLAIM_TIMEOUT_SECONDS,
                    max_attempts=config.KAGGLE_MAX_ATTEMPTS,
                )
                job = repo.get_scene_job(conn, job_id)

            if job is None:
                raise RenderError(UNAVAILABLE_MESSAGE)
            if job["status"] == repo.JOB_COMPLETED:
                return job
            if job["status"] == repo.JOB_FAILED:
                log.warning("scene job %s failed: %s", job_id,
                            redact_token(job.get("error") or ""))
                raise RenderError(job.get("error") or FAILED_MESSAGE)
            if job["status"] == repo.JOB_CANCELLED:
                raise RenderError("This scene’s generation was cancelled.")

            if time.monotonic() > deadline:
                with db.connect() as conn:
                    repo.fail_scene_job(conn, job_id, UNAVAILABLE_MESSAGE)
                log.warning("scene job %s timed out after %ss", job_id,
                            self.job_timeout)
                raise RenderError(UNAVAILABLE_MESSAGE)

            time.sleep(self.poll_seconds)

    def generate(self, spec: SceneSpec, out: Path) -> None:
        problem = self.configuration_error()
        if problem:
            log.warning("wan provider unavailable: %s", problem)
            raise RenderError(UNAVAILABLE_MESSAGE)

        job_id = self._enqueue(spec)
        log.info(
            "queued scene job %s scene=%s %ss %dx%d fps=%d quality=%s",
            job_id, spec.scene_id, spec.seconds, spec.width, spec.height,
            spec.fps, spec.quality,
        )
        job = self._wait_for(job_id)

        staged = storage.storage.localize(job["output_key"] or "")
        if staged is None:
            log.error("scene job %s completed with no readable upload", job_id)
            raise RenderError(UNAVAILABLE_MESSAGE)

        probed = ffmpeg.probe_duration(str(staged))
        if probed is None or probed + DURATION_TOLERANCE_SECONDS < spec.seconds:
            log.error(
                "scene job %s produced %.2fs for a %ss scene; refusing it",
                job_id, probed or 0.0, spec.seconds,
            )
            storage.storage.delete_prefix(job["output_key"])
            raise RenderError(FAILED_MESSAGE)

        ffmpeg.run(ffmpeg.build_normalize_cmd(
            src=str(staged),
            out=str(out),
            width=spec.width,
            height=spec.height,
            seconds=float(spec.seconds),
            fps=spec.fps,
            caption=spec.caption,
        ))
        storage.storage.delete_prefix(job["output_key"])
        log.info("scene job %s normalised into the render pipeline", job_id)
