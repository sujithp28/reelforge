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
