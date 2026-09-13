"""ReelForge Wan 2.1 worker for Kaggle.

Same shape as reelforge_worker.py (the LTX-Video worker) on purpose: same
polling loop, same client, same failure handling. Only the runner differs.

    REELFORGE_API_BASE=https://your-tunnel.example  \
    REELFORGE_WORKER_TOKEN=...                      \
    python wan_worker.py

Why this worker looks different from the LTX one, deliberately:

  * LTX-Video was driven by shelling out to its `inference.py` and, when that
    hit a device-mismatch bug under CPU offload, by hand-patching the
    vendored source inside the subprocess. That approach is not repeated
    here.
  * Wan 2.1 is loaded in-process through Hugging Face `diffusers`, which
    exposes precision as a normal constructor argument: `torch_dtype=`, not
    `dtype=` (this diffusers version, 0.37.1, silently ignores the latter
    with no error — verified the hard way on a real Kaggle T4). A Tesla T4
    (Turing, compute capability 7.5) has no bf16 hardware support — confirmed
    by other frameworks refusing bf16 outright on this GPU — so the pipeline
    is loaded in fp16 throughout except the VAE, which stays fp32. fp16 VAEs
    are a well-known source of NaN latents and black frames across diffusion
    models generally, not a Wan-specific quirk, so this is not a workaround,
    it is the documented way to run this pipeline on affordable hardware.
  * `torch_dtype=torch.float16` on `from_pretrained` does not reliably cast
    every submodule in this diffusers version either: on the verified test
    run, the text encoder came back fp16 but the transformer stayed fp32.
    The transformer is cast explicitly afterward as a result — not a guess,
    a confirmed-necessary extra step.
  * Requires `transformers==4.49.0` pinned exactly. A newer 5.x resolved by
    an unpinned `>=4.49.0` silently reinitialised the text encoder's
    `embed_tokens.weight` at random (missing from the checkpoint per
    `transformers`' own load report) instead of loading it — confirmed on a
    real Kaggle T4, not a theoretical risk. See kaggle/README.md.
  * The model is loaded once, in `warm_up`, and reused for every job. Wan
    2.1 1.3B has no step-distilled checkpoint, so a clip takes materially
    longer than LTX's 8-step distilled config; reloading per job would make
    this much worse.
  * `--dry-run` swaps in a synthetic ffmpeg clip, exactly like the LTX
    worker, so the polling loop and upload path can be exercised with no GPU
    and no weights.
  * `--self-test` generates one tiny clip to a local file and exits, with no
    ReelForge API involved at all. This is the fastest way to confirm the
    model itself runs on a given Kaggle session before wiring it to the
    queue.

Hardware reality on a Kaggle T4:
  * 16 GB VRAM per GPU. The 1.3B transformer needs roughly 8 GB in fp16;
    `enable_model_cpu_offload()` (diffusers' own, tested implementation, not
    a hand-rolled one) keeps peak usage well under that with room to spare.
  * One GPU is used. Two are available, but this model does not need both.
  * Text-to-video only, deliberately. Reference-image conditioning
    (image-to-video) is not wired up yet; ReelForge needs it eventually, and
    Wan 2.1 supports it, but it is intentionally left for a follow-up so the
    first milestone stays small: one real clip, generated reliably.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("reelforge.wan_worker")

# --- configuration ----------------------------------------------------------

API_BASE = os.environ.get("REELFORGE_API_BASE", "").rstrip("/")
WORKER_TOKEN = os.environ.get("REELFORGE_WORKER_TOKEN", "")
WORKER_ID = os.environ.get("REELFORGE_WORKER_ID", "wan-1")
PROVIDER = "wan"

MODEL_ID = os.environ.get("REELFORGE_WAN_MODEL_ID", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
CUDA_DEVICE = os.environ.get("REELFORGE_CUDA_DEVICE", "0")

# Both dimensions must be divisible by 16 for Wan2.1's VAE. 832x480 is the
# model's documented 480p working resolution.
BASE_SHORT_EDGE = int(os.environ.get("REELFORGE_WAN_GEN_HEIGHT", "480"))
BASE_LONG_EDGE = int(os.environ.get("REELFORGE_WAN_GEN_WIDTH", "832"))
GEN_FPS = int(os.environ.get("REELFORGE_WAN_GEN_FPS", "16"))

STEPS_STANDARD = int(os.environ.get("REELFORGE_WAN_STEPS_STANDARD", "30"))
STEPS_HIGH = int(os.environ.get("REELFORGE_WAN_STEPS_HIGH", "40"))
GUIDANCE_SCALE = float(os.environ.get("REELFORGE_WAN_GUIDANCE_SCALE", "5.0"))

POLL_IDLE_SECONDS = float(os.environ.get("REELFORGE_POLL_IDLE_SECONDS", "5"))
MAX_SECONDS_PER_SCENE = int(os.environ.get("REELFORGE_MAX_SCENE_SECONDS", "20"))
GENERATION_TIMEOUT = int(os.environ.get("REELFORGE_GENERATION_TIMEOUT", "2100"))

# Wan2.1's VAE has a temporal compression factor of 4, so a valid frame count
# is 4k+1. Anything else is rejected or silently rounded by the pipeline.
FRAME_QUANTUM = 4

NEGATIVE_PROMPT = (
    "bright tones, overexposed, static, blurred details, subtitles, worst"
    " quality, low quality, deformed, disfigured, extra limbs"
)


# --- geometry and duration helpers (pure, unit-testable) --------------------

def round_to_multiple(value: int, multiple: int = 16) -> int:
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
    """Frames for `seconds`, snapped up to the model's 4k+1 requirement."""
    wanted = max(1, int(round(seconds * fps)))
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
    reference_image: Path | None = None  # accepted, ignored: text-to-video only for now


class WanRunner:
    """Runs Wan 2.1 1.3B in-process through Hugging Face diffusers.

    The pipeline is loaded once in `warm_up` and reused for every job, in
    fp16 (the VAE stays fp32) because a T4 has no bf16 hardware support.
    `enable_model_cpu_offload()` is diffusers' own tested memory
    optimisation, not something reimplemented here.
    """

    def __init__(self, model_id: str, device: str = "0"):
        self.model_id = model_id
        self.device = device
        self._pipeline = None

    def describe(self) -> str:
        return f"Wan 2.1 1.3B ({self.model_id})"

    def warm_up(self) -> None:
        import torch
        from diffusers import WanPipeline

        os.environ.setdefault("CUDA_VISIBLE_DEVICES", self.device)
        torch.cuda.set_device(0)
        log.info("loading %s in fp16 (this can take a few minutes)", self.describe())
        pipeline = WanPipeline.from_pretrained(self.model_id, torch_dtype=torch.float16)
        pipeline.vae.to(torch.float32)
        # torch_dtype above does not reliably reach every submodule; verified
        # on hardware that the transformer needs this explicit cast too.
        pipeline.transformer.to(torch.float16)
        pipeline.enable_model_cpu_offload(device="cuda:0")
        self._pipeline = pipeline
        log.info("%s ready on cuda:0", self.describe())

    def generate(self, request: GenerationRequest, out: Path) -> None:
        from diffusers.utils import export_to_video

        if self._pipeline is None:
            raise RuntimeError("warm_up() was not called before generate()")

        gen_w, gen_h = generation_size(request.width, request.height)
        frames = frame_count(request.seconds)
        steps = steps_for_quality(request.quality)
        log.info(
            "generating %s frames at %dx%d (%.1fs at %dfps), steps=%d",
            frames, gen_w, gen_h, request.seconds, GEN_FPS, steps,
        )
        result = self._pipeline(
            prompt=request.prompt,
            negative_prompt=NEGATIVE_PROMPT,
            height=gen_h,
            width=gen_w,
            num_frames=frames,
            num_inference_steps=steps,
            guidance_scale=GUIDANCE_SCALE,
        ).frames[0]
        export_to_video(result, str(out), fps=GEN_FPS)


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
    def __init__(self, base: str, token: str, worker_id: str = "wan-1"):
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
        query = urllib.parse.urlencode({
            "worker_id": self.worker_id, "provider": PROVIDER,
        })
        body = self._request("GET", f"/api/worker/kaggle/jobs/next?{query}")
        return body.get("job")

    def fetch_reference(self, job_id: str, dest: Path) -> Path | None:
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
        client.fail(
            job_id,
            f"scene is {seconds}s, above this worker's {MAX_SECONDS_PER_SCENE}s limit",
            retryable=False,
        )
        return

    work = Path(tempfile.mkdtemp(prefix="reelforge-wan-job-"))
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
    log.info("polling %s as %s (provider=%s)", client.base, client.worker_id, PROVIDER)
    while max_jobs is None or done < max_jobs:
        try:
            job = client.next_job()
        except Exception as exc:
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


def self_test(runner) -> int:
    """Generate one tiny clip locally, no ReelForge API involved.

    The fastest way to confirm the model actually runs on a given Kaggle
    session before wiring it to the queue at all.
    """
    out = Path("wan_self_test.mp4")
    runner.warm_up()
    started = time.monotonic()
    runner.generate(
        GenerationRequest(
            prompt="a small red boat floating on a calm lake at sunrise",
            seconds=2.0, width=832, height=480, quality="standard",
        ),
        out,
    )
    elapsed = time.monotonic() - started
    if not out.exists() or out.stat().st_size == 0:
        log.error("self-test produced no output file")
        return 1
    log.info("self-test clip written to %s in %.0fs (%d KB)",
              out, elapsed, out.stat().st_size // 1024)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReelForge Wan 2.1 worker")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate synthetic clips with ffmpeg instead of loading a model",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="generate one tiny clip to ./wan_self_test.mp4 and exit;"
             " no ReelForge API involved",
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

    runner = DryRunRunner() if args.dry_run else WanRunner(
        model_id=MODEL_ID, device=CUDA_DEVICE,
    )

    if args.self_test:
        return self_test(runner)

    try:
        client = ReelForgeClient(API_BASE, WORKER_TOKEN, WORKER_ID)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2

    try:
        run_forever(client, runner, args.max_jobs, args.idle_seconds)
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
