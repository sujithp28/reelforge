"""Environment-driven settings.

Everything that differs between local development and a deployed instance is
read here, so swapping SQLite for PostgreSQL or local disk for S3 is a
configuration change rather than a code change.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("REELFORGE_DATA_DIR", BASE_DIR / "data"))

# --- database ---------------------------------------------------------------
# "sqlite" (default, local dev) or "postgres". The repository layer is written
# against whichever dialect this names; see db.sql().
DB_DIALECT = os.environ.get("REELFORGE_DB_DIALECT", "sqlite").lower()
DB_URL = os.environ.get("REELFORGE_DB_URL", "")
SQLITE_PATH = DATA_DIR / "reelforge.db"

# --- object storage ---------------------------------------------------------
# "local" (default) or "s3". Local storage writes under DATA_DIR and is served
# by the app itself; S3 hands back its own URLs.
STORAGE_BACKEND = os.environ.get("REELFORGE_STORAGE", "local").lower()
S3_BUCKET = os.environ.get("REELFORGE_S3_BUCKET", "")
S3_ENDPOINT = os.environ.get("REELFORGE_S3_ENDPOINT", "")
S3_PUBLIC_BASE = os.environ.get("REELFORGE_S3_PUBLIC_BASE", "")

# Path prefix the app serves local media under. Kept stable because the
# frontend treats these as opaque URLs.
MEDIA_URL_PREFIX = "/media"

# --- video generation -------------------------------------------------------
# "mock" (default: ffmpeg stills and type cards, no GPU) or "ltx".
VIDEO_PROVIDER = os.environ.get("REELFORGE_VIDEO_PROVIDER", "mock").lower()

# Base URL of the LTX API. Points at the official hosted service by default;
# override it to target a self-hosted deployment that speaks the same shape.
LTX_ENDPOINT = os.environ.get("REELFORGE_LTX_ENDPOINT", "https://api.ltx.io")
# Never hardcode this. Unset means the LTX provider reports itself unavailable.
LTX_API_KEY = os.environ.get("REELFORGE_LTX_API_KEY", "")
LTX_TIMEOUT_SECONDS = int(os.environ.get("REELFORGE_LTX_TIMEOUT", "900"))

# Customer-facing quality maps to these models. Callers never see model names.
LTX_MODEL_STANDARD = os.environ.get("REELFORGE_LTX_MODEL_STANDARD", "ltx-2-5-fast")
LTX_MODEL_HIGH = os.environ.get("REELFORGE_LTX_MODEL_HIGH", "ltx-2-5-pro")
LTX_RESOLUTION_TIER = os.environ.get("REELFORGE_LTX_RESOLUTION_TIER", "1080p")

# Async job polling. The API documents a minimum of 5 seconds between polls.
LTX_POLL_SECONDS = float(os.environ.get("REELFORGE_LTX_POLL_SECONDS", "6"))
LTX_MAX_POLLS = int(os.environ.get("REELFORGE_LTX_MAX_POLLS", "150"))

# Cost guards. A scene longer than the model's per-clip ceiling is generated in
# chunks; these caps stop a misconfiguration from fanning out into many
# billable calls.
LTX_MAX_CHUNKS_PER_SCENE = int(os.environ.get("REELFORGE_LTX_MAX_CHUNKS", "6"))
LTX_MAX_ATTEMPTS_PER_CHUNK = int(os.environ.get("REELFORGE_LTX_MAX_ATTEMPTS", "2"))

# --- Kaggle GPU beta worker -------------------------------------------------
# The backend never starts a Kaggle notebook. It queues scene jobs; a worker
# running on Kaggle polls for them, generates with open-source LTX-Video, and
# uploads the result back. See kaggle/README.md.
#
# Shared secret for the worker-only endpoints. Unset means the Kaggle
# provider reports itself unavailable. Never logged in full, never returned
# by /health.
KAGGLE_WORKER_TOKEN = os.environ.get("REELFORGE_KAGGLE_WORKER_TOKEN", "")
# How long a scene job may sit unfinished before it is failed. Kaggle sessions
# die without warning, so this is what stops a job wedging a render forever.
KAGGLE_JOB_TIMEOUT_SECONDS = int(os.environ.get("REELFORGE_KAGGLE_JOB_TIMEOUT", "1800"))
# How often the provider checks its queued job.
KAGGLE_POLL_SECONDS = float(os.environ.get("REELFORGE_KAGGLE_POLL_SECONDS", "3"))
# A claimed job whose worker went quiet for this long is returned to the
# queue for another worker.
KAGGLE_CLAIM_TIMEOUT_SECONDS = int(os.environ.get("REELFORGE_KAGGLE_CLAIM_TIMEOUT", "600"))
# Bounded so one bad worker cannot retry a scene forever on free hardware.
KAGGLE_MAX_ATTEMPTS = int(os.environ.get("REELFORGE_KAGGLE_MAX_ATTEMPTS", "3"))
# Upload ceiling for a single generated scene clip.
KAGGLE_MAX_CLIP_BYTES = int(
    os.environ.get("REELFORGE_KAGGLE_MAX_CLIP_BYTES", 200 * 1024 * 1024)
)

# --- Wan 2.1 GPU beta worker -------------------------------------------------
# Same Kaggle-hosted-worker mechanism as the "kaggle" (LTX) provider above,
# reusing REELFORGE_KAGGLE_WORKER_TOKEN for auth since it is one shared secret
# for the Kaggle GPU worker API, not tied to any one model. "wan" is a
# separate provider only because it is a different model and queue lane.
#
# Model id on the Hugging Face Hub. The default is the diffusers-format
# checkpoint, loaded through diffusers.WanPipeline rather than the original
# Wan-Video/Wan2.1 repo: diffusers exposes dtype as a normal constructor
# argument, so a Tesla T4 (Turing, no bf16 support) can run this in fp16
# without patching vendored inference code.
WAN_MODEL_ID = os.environ.get("REELFORGE_WAN_MODEL_ID", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers")
# Generation geometry and step count. Wan2.1 1.3B has no step-distilled
# checkpoint, so this is meaningfully slower per clip than the LTX path.
WAN_GEN_WIDTH = int(os.environ.get("REELFORGE_WAN_GEN_WIDTH", "832"))
WAN_GEN_HEIGHT = int(os.environ.get("REELFORGE_WAN_GEN_HEIGHT", "480"))
WAN_GEN_FPS = int(os.environ.get("REELFORGE_WAN_GEN_FPS", "16"))
WAN_STEPS_STANDARD = int(os.environ.get("REELFORGE_WAN_STEPS_STANDARD", "30"))
WAN_STEPS_HIGH = int(os.environ.get("REELFORGE_WAN_STEPS_HIGH", "40"))
WAN_GUIDANCE_SCALE = float(os.environ.get("REELFORGE_WAN_GUIDANCE_SCALE", "5.0"))
# A scene may wait longer than an LTX job before it fails cleanly: Wan's
# undistilled step count makes even a short clip take minutes, not seconds.
WAN_JOB_TIMEOUT_SECONDS = int(os.environ.get("REELFORGE_WAN_JOB_TIMEOUT", "2400"))

QUALITIES = ("standard", "high")
DEFAULT_QUALITY = os.environ.get("REELFORGE_DEFAULT_QUALITY", "standard").lower()

# --- jobs -------------------------------------------------------------------
# "thread" (default: in-process worker) or "external" (a separate worker
# process drains the queue; the API only enqueues).
JOB_RUNNER = os.environ.get("REELFORGE_JOB_RUNNER", "thread").lower()

# --- limits -----------------------------------------------------------------
MAX_UPLOAD_BYTES = int(os.environ.get("REELFORGE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024))
MIN_REEL_SECONDS = 5
MAX_REEL_SECONDS = 120
MIN_SCENE_SECONDS_ALLOWED = 1
MAX_SCENE_SECONDS_ALLOWED = 30

CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "REELFORGE_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if o.strip()
]
