"""ReelForge GPU worker for Kaggle.

Runs on a Kaggle T4 session, polls ReelForge for scene jobs, generates each
one with open-source LTX-Video, and uploads the result back.

    REELFORGE_API_BASE=https://your-tunnel.example  \
    REELFORGE_WORKER_TOKEN=...                      \
    python reelforge_worker.py

Design notes:

  * The model is loaded once, before the polling loop. Reloading per scene
    would dominate the runtime.
  * Generation runs through `LTXRunner`, which shells out to the official
    LTX-Video repository's `inference.py` with a `--pipeline_config` YAML.
    That is the documented way to run the 2B distilled checkpoint, and it
    keeps us off the project's internal Python API.
  * `--dry-run` swaps in a runner that writes a short synthetic clip with
    ffmpeg, so the whole loop can be exercised with no GPU and no weights.
  * Model, resolution and step counts are configuration, not constants
    scattered through the file.

Hardware reality on a Kaggle T4:
  * 16 GB VRAM per GPU, so the 2B distilled checkpoint (~6.3 GB) is the right
    target. The 13B fp16 checkpoint does not fit comfortably and is not the
    default.
  * A T4 is Turing (sm_75) and has no bfloat16 support, so fp16 is used.
  * One GPU is used. Two are available, but splitting one small model across
    them buys nothing and costs a lot of complexity.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("reelforge.worker")

# --- configuration ----------------------------------------------------------

API_BASE = os.environ.get("REELFORGE_API_BASE", "").rstrip("/")
WORKER_TOKEN = os.environ.get("REELFORGE_WORKER_TOKEN", "")
WORKER_ID = os.environ.get("REELFORGE_WORKER_ID", "kaggle-1")

# Model selection is configuration. The 2B distilled checkpoint is the
# default because it fits a T4 with room to spare.
LTX_REPO_DIR = os.environ.get("REELFORGE_LTX_REPO_DIR", "/kaggle/working/LTX-Video")
_DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "configs", "reelforge-t4-distilled.yaml",
)
LTX_PIPELINE_CONFIG = os.environ.get("REELFORGE_LTX_PIPELINE_CONFIG", _DEFAULT_CONFIG)
CUDA_DEVICE = os.environ.get("REELFORGE_CUDA_DEVICE", "0")

# Generation geometry. Deliberately below the customer's final frame: the
# backend's ffmpeg layer scales and crops to the real aspect ratio, and a
# smaller generation is the difference between fitting a T4 and not.
# Both dimensions must be divisible by 32 for LTX-Video.
BASE_SHORT_EDGE = int(os.environ.get("REELFORGE_GEN_SHORT_EDGE", "480"))
BASE_LONG_EDGE = int(os.environ.get("REELFORGE_GEN_LONG_EDGE", "832"))
GEN_FPS = int(os.environ.get("REELFORGE_GEN_FPS", "25"))

# Distilled checkpoints are guidance- and timestep-distilled: they want
# guidance_scale 1.0 and only a handful of steps.
STEPS_STANDARD = int(os.environ.get("REELFORGE_STEPS_STANDARD", "8"))
STEPS_HIGH = int(os.environ.get("REELFORGE_STEPS_HIGH", "10"))

POLL_IDLE_SECONDS = float(os.environ.get("REELFORGE_POLL_IDLE_SECONDS", "5"))
MAX_SECONDS_PER_SCENE = int(os.environ.get("REELFORGE_MAX_SCENE_SECONDS", "20"))
GENERATION_TIMEOUT = int(os.environ.get("REELFORGE_GENERATION_TIMEOUT", "1500"))

# LTX-Video decodes in latent chunks of 8 frames plus one, so a valid frame
# count is 8k+1. Anything else is rejected or silently rounded by the model.
FRAME_QUANTUM = 8


# --- geometry and duration helpers (pure, unit-testable) --------------------

def round_to_multiple(value: int, multiple: int = 32) -> int:
    """LTX-Video requires width and height divisible by 32."""
    return max(multiple, int(round(value / multiple)) * multiple)


def generation_size(width: int, height: int) -> tuple[int, int]:
    """Pick a T4-friendly generation size with the requested orientation.

    The customer's real frame may be 1080x1920; generating at that size on a
    T4 is not realistic. We generate small in the right orientation and let
    the backend's ffmpeg normalisation scale and crop to the final frame.
    """
    if width > height:
        gen_w, gen_h = BASE_LONG_EDGE, BASE_SHORT_EDGE
    else:
        gen_w, gen_h = BASE_SHORT_EDGE, BASE_LONG_EDGE
    return round_to_multiple(gen_w), round_to_multiple(gen_h)


def frame_count(seconds: float, fps: int = GEN_FPS) -> int:
    """Frames for `seconds`, snapped up to the model's 8k+1 requirement.

    Rounded up rather than down, so the clip always covers the requested
    duration and the backend trims to the exact length. There is no minimum
    billing floor here: unlike the hosted API, a 2-second scene generates
    about 2 seconds.
    """
    wanted = max(1, int(round(seconds * fps)))
    # Solve for the smallest k where 8k+1 >= wanted.
    k = max(1, -(-(wanted - 1) // FRAME_QUANTUM))
    return k * FRAME_QUANTUM + 1


def steps_for_quality(quality: str) -> int:
    return STEPS_HIGH if (quality or "").lower() == "high" else STEPS_STANDARD


# --- model runners ----------------------------------------------------------

@dataclass
class GenerationRequest:
    prompt: str
    seconds: float
    width: int
    height: int
    quality: str
    reference_image: Path | None = None


class LTXRunner:
    """Runs the official LTX-Video inference script.

    Shelling out rather than importing keeps this worker off the project's
    internal Python API, which is not a stability promise. The model still
    loads once per generation in this mode; see `warm_up` for the tradeoff.
    """

    def __init__(self, repo_dir: str, pipeline_config: str, device: str = "0"):
        self.repo_dir = Path(repo_dir)
        self.pipeline_config = pipeline_config
        self.device = device

    def describe(self) -> str:
        return f"LTX-Video ({self.pipeline_config})"

    def warm_up(self) -> None:
        """Fail fast if the checkout or config is missing.

        `inference.py` loads weights per invocation, so there is nothing to
        pre-load here. If per-scene load time proves to dominate, the next
        step is a long-lived in-process pipeline; that is a deliberate
        follow-up rather than something to guess at now.
        """
        script = self.repo_dir / "inference.py"
        config_path = self.repo_dir / self.pipeline_config
        if not script.exists():
            raise RuntimeError(
                f"LTX-Video checkout not found at {self.repo_dir}."
                " See kaggle/README.md for setup."
            )
        if not config_path.exists():
            raise RuntimeError(f"pipeline config not found: {config_path}")
        log.info("using %s on cuda:%s", self.describe(), self.device)

    def generate(self, request: GenerationRequest, out: Path) -> None:
        gen_w, gen_h = generation_size(request.width, request.height)
        frames = frame_count(request.seconds)
        work = Path(tempfile.mkdtemp(prefix="ltx-out-"))

        cmd = [
            sys.executable, "inference.py",
            "--prompt", request.prompt,
            "--height", str(gen_h),
            "--width", str(gen_w),
            "--num_frames", str(frames),
            "--frame_rate", str(GEN_FPS),
            "--seed", str(random.randint(1, 2**31 - 1)),
            "--pipeline_config", self.pipeline_config,
            "--output_path", str(work),
            "--offload_to_cpu", "True",
        ]
        if request.reference_image and request.reference_image.exists():
            # Image-to-video: the still conditions the opening frame.
            cmd += ["--conditioning_media_paths", str(request.reference_image),
                    "--conditioning_start_frames", "0"]

        env = dict(os.environ, CUDA_VISIBLE_DEVICES=self.device)
        log.info(
            "generating %s frames at %dx%d (%.1fs at %dfps), steps=%d",
            frames, gen_w, gen_h, request.seconds, GEN_FPS,
            steps_for_quality(request.quality),
        )
        try:
            proc = subprocess.run(
                cmd, cwd=self.repo_dir, env=env, capture_output=True,
                text=True, timeout=GENERATION_TIMEOUT,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
                raise RuntimeError("inference.py failed: " + " | ".join(tail))
            produced = sorted(work.rglob("*.mp4"))
            if not produced:
                raise RuntimeError("inference.py produced no mp4")
            shutil.move(str(produced[0]), out)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class DryRunRunner:
    """Synthetic clips via ffmpeg. No GPU, no weights, no downloads.

    Exists so the polling loop, the upload path and the backend contract can
    all be exercised end to end before anyone touches a GPU.
    """

    def describe(self) -> str:
        return "dry-run (synthetic ffmpeg clip, no model)"

    def warm_up(self) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("dry-run mode needs ffmpeg on PATH")
        log.info("using %s", self.describe())

    def generate(self, request: GenerationRequest, out: Path) -> None:
        gen_w, gen_h = generation_size(request.width, request.height)
        # Deliberately the wrong frame rate and a slightly wrong length, to
        # prove the backend really does normalise what it receives.
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc2=s={gen_w}x{gen_h}:d={max(1, request.seconds + 1)}:r={GEN_FPS}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
        ], check=True)


# --- ReelForge client -------------------------------------------------------

class ReelForgeClient:
    def __init__(self, base: str, token: str, worker_id: str = "kaggle-1"):
        if not base:
            raise RuntimeError("REELFORGE_API_BASE is not set")
        if not token:
            raise RuntimeError("REELFORGE_WORKER_TOKEN is not set")
        self.base = base.rstrip("/")
        self.token = token
        self.worker_id = worker_id

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            return json.loads(raw) if raw else {}

    def next_job(self) -> dict | None:
        query = urllib.parse.urlencode({"worker_id": self.worker_id})
        body = self._request("GET", f"/api/worker/kaggle/jobs/next?{query}")
        return body.get("job")

    def fetch_reference(self, job_id: str, dest: Path) -> Path | None:
        """Download a job's reference still through the authenticated API."""
        req = urllib.request.Request(
            f"{self.base}/api/worker/kaggle/jobs/{job_id}/reference", method="GET"
        )
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                dest.write_bytes(res.read())
            return dest
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def complete(self, job_id: str, clip: Path) -> dict:
        boundary = "----reelforge-worker"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{clip.name}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode()
        payload = head + clip.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"{self.base}/api/worker/kaggle/jobs/{job_id}/complete",
            data=payload, method="POST",
        )
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type",
                       f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=600) as res:
            return json.loads(res.read() or b"{}")

    def fail(self, job_id: str, detail: str, retryable: bool = True) -> dict:
        return self._request(
            "POST", f"/api/worker/kaggle/jobs/{job_id}/fail",
            {"detail": detail[:2000], "retryable": retryable},
        )


# --- the loop ---------------------------------------------------------------

def handle_job(client: ReelForgeClient, runner, job: dict) -> None:
    job_id = job["job_id"]
    seconds = float(job["seconds"])
    if seconds > MAX_SECONDS_PER_SCENE:
        # Refuse rather than melt: the backend will surface this as a failure.
        client.fail(
            job_id,
            f"scene is {seconds}s, above this worker's {MAX_SECONDS_PER_SCENE}s limit",
            retryable=False,
        )
        return

    work = Path(tempfile.mkdtemp(prefix="reelforge-job-"))
    try:
        reference = None
        if job.get("reference_url"):
            reference = client.fetch_reference(job_id, work / "reference.img")

        clip = work / "scene.mp4"
        started = time.monotonic()
        runner.generate(
            GenerationRequest(
                prompt=job["prompt"],
                seconds=seconds,
                width=int(job["width"]),
                height=int(job["height"]),
                quality=job.get("quality", "standard"),
                reference_image=reference,
            ),
            clip,
        )
        elapsed = time.monotonic() - started
        if not clip.exists() or clip.stat().st_size == 0:
            raise RuntimeError("the model produced no output file")

        client.complete(job_id, clip)
        log.info("job %s completed in %.0fs (%d KB)",
                 job_id, elapsed, clip.stat().st_size // 1024)
    except Exception as exc:
        # Report and keep going. One bad scene must not end the session.
        log.exception("job %s failed", job_id)
        try:
            client.fail(job_id, f"{type(exc).__name__}: {exc}")
        except Exception:
            log.exception("could not report failure for job %s", job_id)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_forever(client: ReelForgeClient, runner, max_jobs: int | None = None,
                idle_seconds: float = POLL_IDLE_SECONDS) -> int:
    runner.warm_up()
    done = 0
    log.info("polling %s as %s", client.base, client.worker_id)
    while max_jobs is None or done < max_jobs:
        try:
            job = client.next_job()
        except Exception as exc:
            # A tunnel restart or a laptop sleeping is normal; wait and retry.
            log.warning("could not reach ReelForge (%s); retrying", exc)
            time.sleep(idle_seconds * 2)
            continue

        if job is None:
            time.sleep(idle_seconds)
            continue

        log.info("claimed job %s: %s", job["job_id"], job["prompt"][:70])
        handle_job(client, runner, job)
        done += 1
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReelForge Kaggle GPU worker")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate synthetic clips with ffmpeg instead of loading a model",
    )
    parser.add_argument(
        "--max-jobs", type=int, default=None,
        help="stop after this many jobs (default: run until stopped)",
    )
    parser.add_argument("--idle-seconds", type=float, default=POLL_IDLE_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    runner = DryRunRunner() if args.dry_run else LTXRunner(
        repo_dir=LTX_REPO_DIR,
        pipeline_config=LTX_PIPELINE_CONFIG,
        device=CUDA_DEVICE,
    )
    try:
        client = ReelForgeClient(API_BASE, WORKER_TOKEN, WORKER_ID)
    except RuntimeError as exc:
        # Never print the token, even when complaining that it is missing.
        log.error("%s", exc)
        return 2

    try:
        run_forever(client, runner, args.max_jobs, args.idle_seconds)
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

