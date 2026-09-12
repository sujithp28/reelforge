"""LTX video generation provider.

Every LTX-specific detail lives in this module. Nothing in main.py, repo.py,
jobs.py, render.py or the frontend knows this provider exists beyond its name.

INTEGRATION METHOD
------------------
The official LTX hosted HTTP API at https://api.ltx.io, using the documented
*asynchronous* job pattern:

    POST /v2/text-to-video   -> 202 with {"id": ...}
    POST /v2/image-to-video  -> 202 with {"id": ...}
    GET  /v2/<endpoint>/<id> -> {"status": ..., "result": {"video_url": ...}}

Chosen over local model inference because ReelForge's backend is a small
FastAPI service with no GPU, and over a third-party reseller because the
first-party API is the current, documented path. `REELFORGE_LTX_ENDPOINT`
makes the base URL configurable, so a self-hosted deployment that speaks the
same shape can be targeted without touching this code.

MODEL CONSTRAINTS, AND HOW EACH IS ABSORBED HERE
------------------------------------------------
The API does not accept the parameters ReelForge offers its customers. Rather
than restricting the product, each gap is closed inside this provider:

  duration      Only even integers are accepted, minimum 6s, and each model
                has a per-clip ceiling (ltx-2-5-fast 20s, ltx-2-5-pro 10s).
                A 7-second scene is impossible. So a scene is planned into
                chunks, each generated at the next allowed length at or above
                what is needed, then trimmed and joined to the exact total.
  aspect ratio  Only 16:9 and 9:16 are offered. 1:1 and 4:5 are produced by
                generating the nearer orientation and centre-cropping.
  frame rate    Only 24, 25, 48 and 50 are offered; the pipeline runs at 30.
                Every clip is resampled on the way in.
  quality       "standard" and "high" map to model ids here. Customers never
                see a model name.

KNOWN LIMITATION: CONSISTENCY
-----------------------------
The API exposes no seed and no character/style reference parameter, so visual
consistency between chunks of one scene, and between scenes, is NOT guaranteed
and is not simulated here. A multi-chunk scene may visibly change between
chunks. Where a scene has a reference still, image-to-video anchors its first
frame, which helps but does not guarantee continuity. Genuine person, product,
character, location or style consistency needs model features that do not
currently exist on this endpoint; `_last_frame_of` is where a future
chunk-to-chunk anchor would attach.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import mimetypes
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config, ffmpeg
from .ffmpeg import RenderError
from .providers import SceneSpec

log = logging.getLogger("reelforge.ltx")

# Documented per-model limits. Durations are even integers from 6 upward.
MIN_CLIP_SECONDS = 6
DURATION_STEP = 2


@dataclass(frozen=True)
class ModelLimits:
    model: str
    max_clip_seconds: int


MODEL_LIMITS = {
    "ltx-2-5-fast": ModelLimits("ltx-2-5-fast", 20),
    "ltx-2-5-pro": ModelLimits("ltx-2-5-pro", 10),
    "ltx-2-3-fast": ModelLimits("ltx-2-3-fast", 10),
    "ltx-2-3-pro": ModelLimits("ltx-2-3-pro", 10),
}
FALLBACK_MAX_CLIP_SECONDS = 10

# Only these two orientations exist on the API.
RESOLUTIONS = {
    "720p": {"landscape": (1280, 720), "portrait": (720, 1280)},
    "1080p": {"landscape": (1920, 1080), "portrait": (1080, 1920)},
    "1440p": {"landscape": (2560, 1440), "portrait": (1440, 2560)},
    "4k": {"landscape": (3840, 2160), "portrait": (2160, 3840)},
}

IMAGE_SUFFIX_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".png": "image/png", ".webp": "image/webp"}


# --- pure planning helpers (unit-tested without any network) ---------------

def allowed_durations(max_clip_seconds: int) -> list[int]:
    return list(range(MIN_CLIP_SECONDS, max_clip_seconds + 1, DURATION_STEP))


def quantize_duration(seconds: float, max_clip_seconds: int) -> int:
    """Smallest allowed generation length that covers `seconds`."""
    target = max(MIN_CLIP_SECONDS, math.ceil(seconds))
    if target % DURATION_STEP:
        target += DURATION_STEP - (target % DURATION_STEP)
    return min(target, max_clip_seconds)


def plan_chunks(seconds: int, max_clip_seconds: int) -> list[tuple[int, float]]:
    """Split a scene into (generate_seconds, keep_seconds) pairs.

    `keep` sums to exactly `seconds`, so trimming each chunk and joining them
    yields the requested scene length. `generate` is always an allowed API
    duration at or above the corresponding `keep`.

    A 7-second scene becomes one 8-second generation trimmed to 7. A
    15-second scene on a 10-second model becomes two 8-second generations
    trimmed to 8 and 7.
    """
    if seconds <= 0:
        raise ValueError("a scene must be at least 1 second long")
    chunks = max(1, math.ceil(seconds / max_clip_seconds))
    if chunks > config.LTX_MAX_CHUNKS_PER_SCENE:
        raise RenderError(
            f"a {seconds}s scene would need {chunks} generations,"
            f" above the configured limit of {config.LTX_MAX_CHUNKS_PER_SCENE}."
            " Shorten the scene or raise REELFORGE_LTX_MAX_CHUNKS."
        )
    base, remainder = divmod(seconds, chunks)
    keeps = [base + (1 if i < remainder else 0) for i in range(chunks)]
    return [(quantize_duration(k, max_clip_seconds), float(k)) for k in keeps]


def model_for_quality(quality: str) -> str:
    return (
        config.LTX_MODEL_HIGH
        if (quality or "").lower() == "high"
        else config.LTX_MODEL_STANDARD
    )


def limits_for(model: str) -> ModelLimits:
    return MODEL_LIMITS.get(model, ModelLimits(model, FALLBACK_MAX_CLIP_SECONDS))


def choose_resolution(width: int, height: int, tier: str | None = None) -> str:
    """Nearest supported generation resolution for a requested frame.

    1:1 and 4:5 are not offered by the API, so they generate portrait and are
    cropped afterwards. Square is treated as portrait because cropping a
    portrait frame to a square loses less of the subject than cropping a
    landscape one.
    """
    table = RESOLUTIONS.get((tier or config.LTX_RESOLUTION_TIER).lower())
    if table is None:
        table = RESOLUTIONS["1080p"]
    orientation = "landscape" if width > height else "portrait"
    w, h = table[orientation]
    return f"{w}x{h}"


def redact(text: str) -> str:
    """Strip anything secret before a message can reach a log or a customer."""
    out = text or ""
    key = config.LTX_API_KEY
    if key:
        out = out.replace(key, "***")
    return out


# --- the provider -----------------------------------------------------------

class LTXGenerator:
    """Generates one scene clip using the LTX hosted API."""

    name = "ltx"

    def __init__(
        self,
        endpoint: str = "",
        api_key: str = "",
        timeout: int = 900,
        opener=None,
    ):
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        # Injected in tests so the request/response handling is exercised
        # without contacting the real API.
        self._opener = opener or urllib.request.urlopen

    # -- availability --------------------------------------------------------

    def available(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def configuration_error(self) -> str | None:
        """Why this provider cannot run, phrased for an operator not a customer."""
        if not self.endpoint:
            return "REELFORGE_LTX_ENDPOINT is not set"
        if not self.api_key:
            return "REELFORGE_LTX_API_KEY is not set"
        return None

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.endpoint}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=self.timeout) as res:
                raw = res.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc) from None
        except urllib.error.URLError as exc:
            raise RenderError(
                f"could not reach the video service ({redact(str(exc.reason))})"
            ) from None

    def _api_error(self, exc: urllib.error.HTTPError) -> RenderError:
        """Turn an HTTP failure into a message safe to store and show.

        The API's own error body is `{"type":"error","error":{"type","message"}}`.
        Its message is operator-facing, so it is logged but never returned:
        customers get a short explanation with no URL, key or provider detail.
        """
        detail = ""
        try:
            payload = json.loads(exc.read() or b"{}")
            detail = str(payload.get("error", {}).get("message", ""))
        except Exception:
            pass
        log.warning("LTX API %s: %s", exc.code, redact(detail) or "no detail")

        if exc.code in (401, 403):
            return RenderError("the video service rejected our credentials")
        if exc.code == 429:
            return RenderError("the video service is rate limiting us; try again shortly")
        if exc.code == 400 or exc.code == 422:
            return RenderError("the video service rejected this scene's settings")
        if 500 <= exc.code < 600:
            return RenderError("the video service is temporarily unavailable")
        return RenderError(f"the video service returned an error ({exc.code})")

    def _submit(self, endpoint: str, payload: dict) -> str:
        body = self._request("POST", f"/v2/{endpoint}", payload)
        job_id = body.get("id")
        if not job_id:
            raise RenderError("the video service did not return a job id")
        return str(job_id)

    def _await_result(self, endpoint: str, job_id: str) -> str:
        """Poll one job to completion and return its video URL."""
        for attempt in range(config.LTX_MAX_POLLS):
            # Jittered so parallel scenes do not poll in lockstep, per the docs.
            time.sleep(config.LTX_POLL_SECONDS + random.uniform(0, 1.5))
            body = self._request("GET", f"/v2/{endpoint}/{job_id}")
            status = str(body.get("status", "")).lower()
            if status == "completed":
                url = (body.get("result") or {}).get("video_url")
                if not url:
                    raise RenderError("the video service returned no video")
                return str(url)
            if status == "failed":
                reason = str((body.get("error") or {}) if isinstance(
                    body.get("error"), dict) else body.get("error") or "")
                log.warning("LTX job %s failed: %s", job_id, redact(reason))
                raise RenderError("the video service could not generate this scene")
            log.debug("LTX job %s %s (poll %d)", job_id, status, attempt + 1)
        raise RenderError("the video service took too long to generate this scene")

    def _download(self, url: str, dest: Path) -> None:
        """Fetch a finished clip. Output URLs expire, so this happens promptly."""
        req = urllib.request.Request(url, method="GET")
        try:
            with self._opener(req, timeout=self.timeout) as res:
                dest.write_bytes(res.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RenderError(
                f"could not download the generated scene ({redact(str(exc))[:120]})"
            ) from None
        if not dest.exists() or dest.stat().st_size == 0:
            raise RenderError("the generated scene was empty")

    # -- image conditioning --------------------------------------------------

    def _image_uri(self, path: Path) -> str:
        """Inline a local reference still as a data URI.

        The API accepts an HTTPS URL, an `ltx://` reference or base64. Local
        development has no public URL for an upload, so it is inlined.
        """
        suffix = path.suffix.lower()
        mime = IMAGE_SUFFIX_TYPES.get(suffix) or mimetypes.guess_type(str(path))[0]
        if not mime or not mime.startswith("image/"):
            raise RenderError(f"unsupported reference image type '{suffix}'")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    # -- generation ----------------------------------------------------------

    def _generate_chunk(
        self, spec: SceneSpec, seconds: int, resolution: str, model: str, dest: Path
    ) -> None:
        payload: dict = {
            "prompt": spec.prompt,
            "model": model,
            "duration": seconds,
            "resolution": resolution,
        }
        endpoint = "text-to-video"
        if spec.image_path and Path(spec.image_path).exists():
            endpoint = "image-to-video"
            payload["image_uri"] = self._image_uri(Path(spec.image_path))

        job_id = self._submit(endpoint, payload)
        log.info(
            "LTX %s job=%s scene=%s model=%s duration=%ss resolution=%s",
            endpoint, job_id, spec.scene_id, model, seconds, resolution,
        )
        url = self._await_result(endpoint, job_id)
        self._download(url, dest)

    def generate(self, spec: SceneSpec, out: Path) -> None:
        """Produce one clip at exactly spec.seconds and spec resolution.

        Chunking, trimming and cropping all happen here, so render.py receives
        the same thing it would from the mock provider.
        """
        problem = self.configuration_error()
        if problem:
            raise RenderError(f"the LTX provider is not configured: {problem}")

        model = model_for_quality(getattr(spec, "quality", "standard"))
        limits = limits_for(model)
        resolution = choose_resolution(spec.width, spec.height)
        plan = plan_chunks(int(spec.seconds), limits.max_clip_seconds)

        work = out.parent / f"{out.stem}_ltx"
        work.mkdir(parents=True, exist_ok=True)
        pieces: list[Path] = []
        try:
            for i, (generate_seconds, keep_seconds) in enumerate(plan):
                raw = work / f"chunk_{i:02d}_raw.mp4"
                self._generate_with_retry(
                    spec, generate_seconds, resolution, model, raw, i, len(plan)
                )
                # Conform to the scene's real frame, frame rate and length.
                piece = work / f"chunk_{i:02d}.mp4"
                ffmpeg.run(ffmpeg.build_normalize_cmd(
                    src=str(raw), out=str(piece),
                    width=spec.width, height=spec.height,
                    seconds=keep_seconds, fps=spec.fps,
                    caption=spec.caption if i == 0 else None,
                ))
                pieces.append(piece)
                raw.unlink(missing_ok=True)

            if len(pieces) == 1:
                pieces[0].replace(out)
            else:
                listing = work / "chunks.txt"
                listing.write_text(
                    ffmpeg.build_concat_list([str(p) for p in pieces]),
                    encoding="utf-8",
                )
                ffmpeg.run(ffmpeg.build_concat_cmd(str(listing), str(out)))
        finally:
            for leftover in sorted(work.rglob("*"), reverse=True):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                work.rmdir()
            except OSError:
                pass

    def _generate_with_retry(
        self, spec: SceneSpec, seconds: int, resolution: str, model: str,
        dest: Path, index: int, total: int,
    ) -> None:
        """Retry one chunk a bounded number of times.

        Bounded deliberately: an unbounded retry on a billable endpoint is how
        a render loop turns into a cost incident.
        """
        attempts = max(1, config.LTX_MAX_ATTEMPTS_PER_CHUNK)
        last: RenderError | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._generate_chunk(spec, seconds, resolution, model, dest)
                return
            except RenderError as exc:
                last = exc
                log.warning(
                    "scene=%s chunk %d/%d attempt %d/%d failed: %s",
                    spec.scene_id, index + 1, total, attempt, attempts, exc,
                )
                if attempt < attempts:
                    time.sleep(min(30, 5 * attempt))
        raise last or RenderError("the video service could not generate this scene")
