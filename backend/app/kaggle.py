"""Kaggle GPU beta provider.

Every Kaggle-specific detail lives in this module. render.py, jobs.py and the
frontend know only that a provider named "kaggle" exists.

HOW IT WORKS
------------
The backend never starts or contacts a Kaggle notebook. Control runs the
other way:

    render.py  ->  KaggleGenerator.generate()
                       enqueues a scene_jobs row, then waits
    Kaggle worker  ->  GET  /api/worker/kaggle/jobs/next        (claims it)
                       GET  /api/worker/kaggle/jobs/{id}/reference
                       POST /api/worker/kaggle/jobs/{id}/complete  (uploads mp4)
    KaggleGenerator    sees `completed`, normalises the upload with ffmpeg,
                       writes it to the output path render.py asked for

That inversion is deliberate: a Kaggle session has no stable inbound address
and can vanish at any moment, so it polls us and we never depend on reaching
it.

WHAT IS NOT TRUSTED
-------------------
The uploaded clip's duration, resolution and frame rate are all treated as
suggestions. Every clip goes through the existing ffmpeg normalisation, which
is what guarantees render.py receives exactly the SceneSpec it asked for.
Without that, one worker running a different model config would silently
corrupt the concatenation.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import config, db, ffmpeg, repo, storage
from .ffmpeg import RenderError
from .providers import SceneSpec

log = logging.getLogger("reelforge.kaggle")

# Shown to customers. Says what to do, names nothing internal.
UNAVAILABLE_MESSAGE = (
    "AI generation is temporarily unavailable. Please try this scene again."
)
FAILED_MESSAGE = "This scene couldn’t be generated. Please try again."

# How far short of the requested length an uploaded clip may be. ffmpeg can
# trim a long clip to length but cannot extend a short one, so a materially
# short upload has to be refused rather than silently shortening the scene.
DURATION_TOLERANCE_SECONDS = 0.25


def staging_key(job_id: str) -> str:
    """Where a worker's upload is staged before normalisation."""
    return f"kaggle/{job_id}.mp4"


def redact_token(text: str) -> str:
    """Remove the worker token from anything that might be logged."""
    out = text or ""
    token = config.KAGGLE_WORKER_TOKEN
    if token and len(token) >= 4:
        out = out.replace(token, "***")
    return out


def token_fingerprint() -> str:
    """A safe way to refer to the configured token in logs.

    Never the token itself, and never enough of it to be useful: just the
    length and last three characters so an operator can tell two keys apart.
    """
    token = config.KAGGLE_WORKER_TOKEN
    if not token:
        return "unset"
    return f"len={len(token)} ...{token[-3:]}"


class KaggleGenerator:
    """Queues a scene for an external GPU worker and waits for the result."""

    name = "kaggle"

    def __init__(
        self,
        worker_token: str = "",
        job_timeout: int = 1800,
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
        """Create the queue row. Requires the scene to still exist."""
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

            # One row per generate() call, so every GPU-second is attributable
            # to an explicit job rather than an accidental loop.
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
        """Poll the queue row until it finishes, is cancelled, or times out."""
        deadline = time.monotonic() + self.job_timeout
        while True:
            # Deliberately no open connection while sleeping: a render can
            # wait many minutes and must not hold a write lock meanwhile.
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
                # The stored reason is already customer-safe; the worker's raw
                # detail never reaches this column.
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
            log.warning("kaggle provider unavailable: %s", problem)
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

        # Second line of defence. The upload endpoint already refuses a short
        # clip, but this is the last point before the file enters the render
        # pipeline, and a short scene here would corrupt the whole reel's
        # timing silently rather than loudly.
        probed = ffmpeg.probe_duration(str(staged))
        if probed is None or probed + DURATION_TOLERANCE_SECONDS < spec.seconds:
            log.error(
                "scene job %s produced %.2fs for a %ss scene; refusing it",
                job_id, probed or 0.0, spec.seconds,
            )
            storage.storage.delete_prefix(job["output_key"])
            raise RenderError(FAILED_MESSAGE)

        # The worker's dimensions, length and frame rate are not trusted: this
        # is what makes the clip safe to concatenate with the others.
        ffmpeg.run(ffmpeg.build_normalize_cmd(
            src=str(staged),
            out=str(out),
            width=spec.width,
            height=spec.height,
            seconds=float(spec.seconds),
            fps=spec.fps,
            caption=spec.caption,
        ))
        # The staged upload has served its purpose; render.py caches the
        # normalised clip instead.
        storage.storage.delete_prefix(job["output_key"])
        log.info("scene job %s normalised into the render pipeline", job_id)
