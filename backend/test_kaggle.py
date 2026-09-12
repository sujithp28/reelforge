"""Kaggle provider and worker API checks.

Needs no Kaggle account, no GPU, no model weights and no internet. The
external worker is faked: a small in-process class that calls the same HTTP
endpoints a real Kaggle session would.

    python test_kaggle.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="reelforge-kaggle-")
os.environ["REELFORGE_DATA_DIR"] = _TMP
os.environ["REELFORGE_JOB_RUNNER"] = "external"
os.environ["REELFORGE_VIDEO_PROVIDER"] = "mock"
os.environ["REELFORGE_KAGGLE_WORKER_TOKEN"] = "test-worker-token-do-not-use"
os.environ["REELFORGE_KAGGLE_POLL_SECONDS"] = "0.05"
os.environ["REELFORGE_KAGGLE_JOB_TIMEOUT"] = "6"
# Long on purpose. The worker API requeues stale claims on every poll, so a
# short global timeout would rescue jobs other tests had just claimed and make
# the whole file flaky. The stale-recovery tests drive that logic directly with
# their own timeout instead.
os.environ["REELFORGE_KAGGLE_CLAIM_TIMEOUT"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, ffmpeg, kaggle, providers, render, repo, storage  # noqa: E402
from app.ffmpeg import RenderError  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import SceneSpec  # noqa: E402

# Make the worker module importable without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kaggle"))
import reelforge_worker as worker  # noqa: E402

client = TestClient(app)
TMP = Path(_TMP)
TOKEN = config.KAGGLE_WORKER_TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HAVE_FFMPEG = ffmpeg.ffmpeg_available()

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f9d0000000049454e44ae42"
    "6082"
)


# --- helpers ----------------------------------------------------------------

def make_project(**overrides):
    body = {"idea": "a quiet courtyard at dusk", "category": "Real Estate",
            "duration": 12, "aspect_ratio": "9:16"}
    body.update(overrides)
    res = client.post("/api/projects", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def make_clip(seconds=3, width=480, height=832, fps=25) -> bytes:
    """A real mp4 standing in for worker output, at a deliberately wrong fps."""
    dest = TMP / f"clip_{seconds}_{width}x{height}_{fps}.mp4"
    if not dest.exists():
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", f"testsrc2=s={width}x{height}:d={seconds}:r={fps}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest),
        ], check=True)
    return dest.read_bytes()


def drain_queue():
    """Claim everything pending so the next enqueued job is the only one.

    `next_job()` hands out the OLDEST pending job, so without this a test
    would be handed a leftover from an earlier test rather than its own.
    """
    drainer = FakeWorker("drainer")
    while drainer.next_job() is not None:
        pass


def claim_job(job_id, w=None):
    """Claim a specific job, draining anything queued ahead of it.

    Depending on queue order made these tests fragile: `next_job()` hands out
    the oldest pending job, which may belong to another test.
    """
    w = w or FakeWorker()
    for _ in range(50):
        job = w.next_job()
        if job is None:
            raise AssertionError(f"{job_id} was never offered to the worker")
        if job["job_id"] == job_id:
            return job
    raise AssertionError(f"{job_id} was never reached in the queue")


def enqueue_job(project, scene=None, seconds=3, quality="standard", drain=True):
    """Queue a scene job the way KaggleGenerator does."""
    scene = scene or project["scenes"][0]
    if drain:
        drain_queue()
    with db.connect() as conn:
        return repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene["id"],
            provider="kaggle", prompt=scene["prompt"], duration=seconds,
            width=1080, height=1920, fps=30, quality=quality,
        )


class FakeWorker:
    """Stands in for the Kaggle session, over the real HTTP endpoints."""

    def __init__(self, worker_id="fake-1", token=TOKEN):
        self.worker_id = worker_id
        self.headers = {"Authorization": f"Bearer {token}"}

    def next_job(self):
        res = client.get("/api/worker/kaggle/jobs/next",
                         params={"worker_id": self.worker_id},
                         headers=self.headers)
        assert res.status_code == 200, res.text
        return res.json()["job"]

    def complete(self, job_id, payload=None, content_type="video/mp4"):
        return client.post(
            f"/api/worker/kaggle/jobs/{job_id}/complete",
            headers=self.headers,
            files={"file": ("scene.mp4", payload if payload is not None
                            else make_clip(), content_type)},
        )

    def fail(self, job_id, detail="CUDA out of memory at 0x7f", retryable=True):
        return client.post(f"/api/worker/kaggle/jobs/{job_id}/fail",
                           headers=self.headers,
                           json={"detail": detail, "retryable": retryable})

    def reference(self, job_id):
        return client.get(f"/api/worker/kaggle/jobs/{job_id}/reference",
                          headers=self.headers)


# --- 1. provider availability ----------------------------------------------

def test_kaggle_unavailable_without_worker_token():
    no_token = kaggle.KaggleGenerator(worker_token="")
    assert no_token.available() is False
    assert "WORKER_TOKEN" in (no_token.configuration_error() or "")

    configured = kaggle.KaggleGenerator(worker_token="x")
    assert configured.available() is True
    assert configured.configuration_error() is None


def test_kaggle_refuses_to_generate_without_a_token():
    spec = SceneSpec(scene_id="s", index=0, title="t", prompt="p", caption=None,
                     seconds=3, width=1080, height=1920)
    try:
        kaggle.KaggleGenerator(worker_token="").generate(spec, TMP / "never.mp4")
    except RenderError as exc:
        # Customer-safe: no token, no host, no internals.
        assert "temporarily unavailable" in str(exc)
        assert "TOKEN" not in str(exc)
    else:
        raise AssertionError("generated with no worker token")


# --- 2. provider discovery and health --------------------------------------

def test_provider_discovery_includes_all_three():
    assert providers.PROVIDER_NAMES == ("mock", "ltx", "kaggle")
    available = providers.available_providers()
    assert set(available) == {"mock", "ltx", "kaggle"}
    assert available["mock"] is True, "mock must never stop working"
    assert available["kaggle"] is True, "a token is configured in this test"
    assert providers.build_generator("kaggle").name == "kaggle"
    # The existing providers must still resolve.
    assert providers.build_generator("mock").name == "mock"
    assert providers.build_generator("ltx").name == "ltx"


def test_health_exposes_kaggle_but_never_the_token():
    body = client.get("/health").json()
    assert "kaggle" in body["providers"]
    assert body["worker_api_enabled"] is True
    assert "scene_job_queue" in body
    raw = json.dumps(body)
    assert TOKEN not in raw, "the worker token leaked through /health"
    for field in ("worker_token", "KAGGLE_WORKER_TOKEN"):
        assert field not in raw, field


# --- 3. worker authentication ----------------------------------------------

def test_worker_routes_require_the_token():
    project = make_project()
    job_id = enqueue_job(project)
    routes = [
        ("get", "/api/worker/kaggle/jobs/next", {}),
        ("get", f"/api/worker/kaggle/jobs/{job_id}/reference", {}),
        ("post", f"/api/worker/kaggle/jobs/{job_id}/fail", {"json": {}}),
    ]
    for method, path, kwargs in routes:
        # No header at all.
        assert getattr(client, method)(path, **kwargs).status_code == 401, path
        # Wrong token.
        bad = getattr(client, method)(
            path, headers={"Authorization": "Bearer wrong"}, **kwargs)
        assert bad.status_code == 401, path
        # Right token, wrong scheme.
        basic = getattr(client, method)(
            path, headers={"Authorization": f"Basic {TOKEN}"}, **kwargs)
        assert basic.status_code == 401, path

    upload = client.post(f"/api/worker/kaggle/jobs/{job_id}/complete",
                         files={"file": ("x.mp4", b"x", "video/mp4")})
    assert upload.status_code == 401


def test_auth_failure_never_echoes_a_token():
    res = client.get("/api/worker/kaggle/jobs/next",
                     headers={"Authorization": "Bearer wrong-but-secret"})
    assert res.status_code == 401
    assert "wrong-but-secret" not in res.text
    assert TOKEN not in res.text


# --- 4 & 5. claiming, and no double claiming -------------------------------

def test_next_job_claims_and_hides_internals():
    project = make_project()
    job_id = enqueue_job(project, seconds=4)
    job = claim_job(job_id)
    assert job is not None and job["job_id"] == job_id

    # Only what the worker needs.
    assert set(job) == {"job_id", "scene_id", "prompt", "seconds", "width",
                        "height", "fps", "quality", "attempt", "reference_url"}
    assert job["seconds"] == 4 and job["fps"] == 30
    assert job["width"] == 1080 and job["height"] == 1920
    # No paths, no storage keys.
    raw = json.dumps(job)
    for leak in ("storage_key", "output_key", "reference_key", _TMP, "clips/"):
        assert leak not in raw, leak

    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_CLAIMED
    assert row["claimed_by"] == "fake-1"
    assert row["attempts"] == 1


def test_two_workers_never_claim_the_same_job():
    project = make_project()
    job_id = enqueue_job(project)
    first = FakeWorker("w1").next_job()
    second = FakeWorker("w2").next_job()
    assert first is not None and first["job_id"] == job_id
    assert second is None, "a second worker claimed an already-claimed job"


def test_concurrent_claims_are_serialised():
    """Hammer the claim path from threads; each job may go to one worker only."""
    project = make_project(duration=30)
    drain_queue()
    ids = {enqueue_job(project, scene=s, drain=False)
           for s in project["scenes"][:3]}
    claimed: list[str] = []
    lock = threading.Lock()

    def grab(n):
        w = FakeWorker(f"race-{n}")
        for _ in range(3):
            job = w.next_job()
            if job:
                with lock:
                    claimed.append(job["job_id"])

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    mine = [c for c in claimed if c in ids]
    assert len(mine) == len(set(mine)), f"a job was claimed twice: {claimed}"
    assert set(mine) == ids, "not every queued job was handed out"


def test_no_work_returns_null_not_an_error():
    # Drain anything left pending, then ask again.
    w = FakeWorker("drainer")
    while w.next_job() is not None:
        pass
    assert w.next_job() is None


# --- 6. successful completion ----------------------------------------------

def test_successful_upload_completes_the_job():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    project = make_project()
    job_id = enqueue_job(project)
    w = FakeWorker()
    claim_job(job_id, w)
    res = w.complete(job_id)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == repo.JOB_COMPLETED

    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_COMPLETED
    assert row["output_key"] == storage.kaggle_staging_key(job_id)
    assert row["completed_at"]
    # The worker cannot choose where its file lands.
    assert storage.storage.localize(row["output_key"]) is not None


def test_upload_is_rejected_unless_claimed():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    project = make_project()
    job_id = enqueue_job(project)
    # Never claimed, so an upload must not be accepted.
    assert FakeWorker().complete(job_id).status_code == 409


# --- 7. failed generation --------------------------------------------------

def test_worker_failure_is_retried_then_failed_safely():
    project = make_project()
    job_id = enqueue_job(project)
    w = FakeWorker()

    # First failure is retryable, so the job returns to the queue.
    claim_job(job_id, w)
    res = w.fail(job_id, detail="CUDA out of memory; /kaggle/working/model.safetensors")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == repo.JOB_PENDING

    # Exhaust the attempts.
    for _ in range(config.KAGGLE_MAX_ATTEMPTS + 1):
        with db.connect() as conn:
            row = repo.get_scene_job(conn, job_id)
        if row["status"] in repo.TERMINAL_SCENE_JOB_STATUSES:
            break
        claim_job(job_id, w)
        w.fail(job_id, detail="CUDA out of memory")

    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_FAILED, row["status"]
    # The stored reason is customer-safe: no CUDA, no paths, no model names.
    stored = (row["error"] or "").lower()
    for leak in ("cuda", "safetensors", "/kaggle/", "traceback", "memory"):
        assert leak not in stored, f"{leak!r} leaked into a customer message"


def test_non_retryable_failure_fails_immediately():
    project = make_project()
    job_id = enqueue_job(project)
    w = FakeWorker()
    claim_job(job_id, w)
    res = w.fail(job_id, detail="scene too long", retryable=False)
    assert res.json()["status"] == repo.JOB_FAILED
    # And a second report is refused rather than reopening it.
    assert w.fail(job_id).status_code == 409


# --- 8. timeout ------------------------------------------------------------

def test_generate_times_out_cleanly_when_no_worker_appears():
    """No worker ever claims it, so generate() must give up and say so."""
    project = make_project()
    scene = project["scenes"][0]
    drain_queue()
    generator = kaggle.KaggleGenerator(
        worker_token=TOKEN, job_timeout=1, poll_seconds=0.05
    )
    spec = SceneSpec(
        scene_id=scene["id"], index=0, title=scene["title"],
        prompt=scene["prompt"], caption=None, seconds=3,
        width=1080, height=1920,
    )
    started = time.monotonic()
    try:
        generator.generate(spec, TMP / "timeout.mp4")
    except RenderError as exc:
        assert "temporarily unavailable" in str(exc), str(exc)
        assert "try this scene again" in str(exc).lower()
    else:
        raise AssertionError("a job with no worker was treated as success")
    assert time.monotonic() - started < 20, "timeout took far too long"

    # And the job is left in a terminal state, not stuck pending forever.
    with db.connect() as conn:
        rows = conn.execute(
            db.sql("SELECT status FROM scene_jobs WHERE scene_id = ?"),
            (scene["id"],),
        ).fetchall()
    assert any(r["status"] == repo.JOB_FAILED for r in rows), [dict(r) for r in rows]


# --- 9. stale claimed job recovery -----------------------------------------

def test_stale_claimed_job_is_requeued():
    project = make_project()
    job_id = enqueue_job(project)
    claim_job(job_id, FakeWorker("vanishing"))
    with db.connect() as conn:
        assert repo.get_scene_job(conn, job_id)["status"] == repo.JOB_CLAIMED

    # Pretend the worker claimed it long ago and then died.
    with db.connect() as conn:
        conn.execute(
            db.sql("UPDATE scene_jobs SET claimed_at = ? WHERE id = ?"),
            ("2000-01-01 00:00:00", job_id),
        )
        result = repo.requeue_stale_scene_jobs(
            conn, claim_timeout_seconds=1, max_attempts=5
        )
    assert result["requeued"] >= 1, result
    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_PENDING
    assert row["claimed_by"] is None and row["claimed_at"] is None
    # Another worker can now pick it up. Claimed by id, because the same
    # requeue pass also rescues jobs left claimed by earlier tests.
    assert claim_job(job_id, FakeWorker("rescuer"))["job_id"] == job_id


def test_stale_job_out_of_attempts_fails_instead_of_looping():
    project = make_project()
    job_id = enqueue_job(project)
    with db.connect() as conn:
        conn.execute(
            db.sql("UPDATE scene_jobs SET status = ?, attempts = 9,"
                   " claimed_at = ? WHERE id = ?"),
            (repo.JOB_CLAIMED, "2000-01-01 00:00:00", job_id),
        )
        result = repo.requeue_stale_scene_jobs(
            conn, claim_timeout_seconds=1, max_attempts=3
        )
    assert result["failed"] >= 1, result
    with db.connect() as conn:
        assert repo.get_scene_job(conn, job_id)["status"] == repo.JOB_FAILED


# --- 10. reference image authorization -------------------------------------

def test_reference_image_requires_auth_and_hides_paths():
    project = make_project()
    upload = client.post(f"/api/projects/{project['id']}/uploads",
                         files={"file": ("ref.png", PNG_1PX, "image/png")})
    assert upload.status_code == 201
    scene = upload.json()["scenes"][0]

    drain_queue()
    with db.connect() as conn:
        row = conn.execute(
            db.sql("SELECT storage_key FROM assets WHERE project_id = ?"),
            (project["id"],),
        ).fetchone()
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene["id"],
            provider="kaggle", prompt="p", duration=3, width=1080, height=1920,
            fps=30, quality="standard", reference_key=row["storage_key"],
        )

    job = claim_job(job_id)
    assert job["job_id"] == job_id
    # An endpoint to call, not a path to read.
    assert job["reference_url"] == f"/api/worker/kaggle/jobs/{job_id}/reference"

    got = FakeWorker().reference(job_id)
    assert got.status_code == 200
    assert got.content == PNG_1PX

    # Unauthenticated access is refused.
    assert client.get(
        f"/api/worker/kaggle/jobs/{job_id}/reference"
    ).status_code == 401


def test_reference_missing_is_404_not_a_path_error():
    project = make_project()
    job_id = enqueue_job(project)          # no reference image
    res = FakeWorker().reference(job_id)
    assert res.status_code == 404
    assert _TMP not in res.text


# --- 11. invalid job ids ---------------------------------------------------

def test_invalid_job_ids_are_404():
    w = FakeWorker()
    assert w.reference("sjob_nope").status_code == 404
    assert w.fail("sjob_nope").status_code == 404
    assert w.complete("sjob_nope", payload=b"not-video").status_code == 404
    # Traversal-shaped ids must not escape anything either.
    assert w.reference("..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)


# --- 12. invalid uploads ---------------------------------------------------

def test_invalid_uploads_are_rejected():
    project = make_project()
    job_id = enqueue_job(project)
    w = FakeWorker()
    claim_job(job_id, w)

    # Not decodable video.
    bad = w.complete(job_id, payload=b"this is definitely not an mp4")
    assert bad.status_code == 422, bad.text
    # The job must still be claimed, so a real clip can follow.
    with db.connect() as conn:
        assert repo.get_scene_job(conn, job_id)["status"] == repo.JOB_CLAIMED

    # Wrong content type.
    assert w.complete(job_id, payload=b"x", content_type="text/html").status_code == 415
    # Empty body.
    assert w.complete(job_id, payload=b"").status_code == 422


def test_short_upload_is_rejected_rather_than_shortening_the_scene():
    """ffmpeg can trim a long clip but cannot extend a short one.

    Accepting a short clip would silently produce a reel shorter than the
    storyboard, so the upload is refused and the job stays claimable.
    """
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    project = make_project()
    job_id = enqueue_job(project, seconds=6)
    w = FakeWorker()
    claim_job(job_id, w)
    res = w.complete(job_id, payload=make_clip(seconds=2))
    assert res.status_code == 422, res.text
    assert "needs" in res.json()["detail"]
    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_CLAIMED, "a short upload ended the job"
    assert row["output_key"] is None

    # A long-enough clip is then accepted for the same job.
    ok = w.complete(job_id, payload=make_clip(seconds=7))
    assert ok.status_code == 200, ok.text


def test_oversized_upload_is_rejected():
    project = make_project()
    job_id = enqueue_job(project)
    w = FakeWorker()
    claim_job(job_id, w)
    oversized = b"\x00" * (config.KAGGLE_MAX_CLIP_BYTES + 2048)
    assert w.complete(job_id, payload=oversized).status_code == 413


# --- 13. the full provider round trip --------------------------------------

def test_generate_round_trip_normalises_the_worker_clip():
    """The contract: whatever the worker sends, render.py gets the SceneSpec.

    The fake worker uploads 6 seconds of 480x832 at 25fps for a 5-second
    1080x1350 request: wrong size, wrong frame rate, and longer than asked
    for, which is what a real worker does because the model quantises frames
    upward. All three must be corrected on the way in.
    """
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    project = make_project()
    scene = project["scenes"][0]
    drain_queue()
    generator = kaggle.KaggleGenerator(
        worker_token=TOKEN, job_timeout=30, poll_seconds=0.05
    )
    spec = SceneSpec(
        scene_id=scene["id"], index=0, title=scene["title"],
        prompt=scene["prompt"], caption=None, seconds=5,
        width=1080, height=1350, quality="standard",
    )
    out = TMP / "round_trip.mp4"

    def fake_worker_session():
        w = FakeWorker("round-trip")
        for _ in range(200):
            job = w.next_job()
            if job:
                w.complete(job["job_id"],
                           payload=make_clip(seconds=6, width=480, height=832, fps=25))
                return
            time.sleep(0.05)

    thread = threading.Thread(target=fake_worker_session, daemon=True)
    thread.start()
    generator.generate(spec, out)
    thread.join(timeout=5)

    assert out.exists(), "no clip reached the render pipeline"
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=width,height,r_frame_rate,codec_name,codec_type:format=duration",
         "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)
    stream = info["streams"][0]
    # Exact SceneSpec contract, not what the worker happened to send.
    assert (stream["width"], stream["height"]) == (1080, 1350), stream
    assert stream["r_frame_rate"] == "30/1", stream["r_frame_rate"]
    assert stream["codec_name"] == "h264"
    assert abs(float(info["format"]["duration"]) - 5) < 0.4
    # No audio track, matching what the concat step expects.
    assert all(s["codec_type"] != "audio" for s in info["streams"]), info["streams"]

    # The staging upload is cleaned up once normalised.
    with db.connect() as conn:
        row = conn.execute(
            db.sql("SELECT output_key FROM scene_jobs WHERE scene_id = ?"
                   " ORDER BY created_at DESC LIMIT 1"),
            (scene["id"],),
        ).fetchone()
    assert storage.storage.localize(row["output_key"]) is None


# --- 14. scene cache and retry still work ----------------------------------

class CountingKaggleLike:
    """A kaggle-shaped generator that counts calls, for cache assertions."""

    name = "kaggle"

    def __init__(self, fail_indices=()):
        self.fail_indices = set(fail_indices)
        self.calls: list[str] = []

    def available(self):
        return True

    def generate(self, spec, out):
        self.calls.append(spec.scene_id)
        if spec.index in self.fail_indices:
            raise RenderError(kaggle.FAILED_MESSAGE)
        ffmpeg.run(ffmpeg.build_card_clip_cmd(
            out=str(out), seconds=spec.seconds, width=spec.width,
            height=spec.height, caption=spec.title, index=spec.index,
        ))


class MemoryHooks:
    def __init__(self):
        self.cache: dict[str, Path] = {}
        self.reused: list[str] = []
        self.failed: list[str] = []

    def should_cancel(self):
        return False

    def cached_clip(self, scene_id, fingerprint):
        hit = self.cache.get(f"{scene_id}:{fingerprint}")
        if hit:
            self.reused.append(scene_id)
        return hit

    def clip_succeeded(self, scene_id, fingerprint, clip):
        kept = TMP / f"cache_{scene_id}.mp4"
        shutil.copy2(clip, kept)
        self.cache[f"{scene_id}:{fingerprint}"] = kept

    def clip_failed(self, scene_id, fingerprint, message):
        self.failed.append(scene_id)

    def generation_logged(self, scene_id, status, seconds, elapsed_ms, error):
        pass


def test_scene_cache_and_retry_survive_the_new_provider():
    """Scene 2 failing must not cost Scene 1 a regeneration."""
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    scenes = [
        {"id": f"kscene_{i}", "position": i, "title": f"Scene {i + 1}",
         "prompt": f"prompt {i}", "duration": 2, "caption": None}
        for i in range(5)
    ]
    hooks = MemoryHooks()

    first = CountingKaggleLike(fail_indices={1})
    try:
        render.render_reel(
            scenes=scenes, images={}, aspect_ratio="9:16",
            work_dir=TMP / "kcache1", out_path=TMP / "kcache1.mp4",
            generator=first, hooks=hooks,
        )
    except render.SceneGenerationError as exc:
        assert "Scene 2" in str(exc), str(exc)
    else:
        raise AssertionError("a failed scene did not fail the render")
    assert len(first.calls) == 5
    assert hooks.failed == ["kscene_1"]

    # Retry: only the failed scene is generated again.
    second = CountingKaggleLike()
    out = TMP / "kcache2.mp4"
    render.render_reel(
        scenes=scenes, images={}, aspect_ratio="9:16",
        work_dir=TMP / "kcache2", out_path=out,
        generator=second, hooks=hooks,
    )
    assert second.calls == ["kscene_1"], second.calls
    assert sorted(hooks.reused) == ["kscene_0", "kscene_2", "kscene_3", "kscene_4"]
    assert out.exists()
    assert abs((ffmpeg.probe_duration(str(out)) or 0) - 10) < 0.8


def test_retry_reuses_the_providers_of_the_last_render():
    """Retry must not silently switch providers.

    The provider is part of the scene cache fingerprint, so retrying with the
    server default instead of the provider the reel was rendered with would
    invalidate every cached clip and regenerate the whole reel. This is a
    regression test: that is exactly what happened in an end-to-end run.
    """
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    project = make_project()
    # Render explicitly with kaggle even though the server default is mock.
    started = client.post(f"/api/projects/{project['id']}/render",
                          json={"provider": "kaggle"})
    assert started.status_code == 202, started.text
    assert started.json()["provider"] == "kaggle"

    # The job runner is "external" in these tests, so the render never
    # finishes on its own. Release it so retry is not simply refused with 409.
    with db.connect() as conn:
        repo.mark_ready(conn, project["id"], "renders/fake.mp4")

    retried = client.post(
        f"/api/projects/{project['id']}/scenes/{project['scenes'][0]['id']}/retry"
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["provider"] == "kaggle", (
        "retry fell back to the server default and would rebuild every scene"
    )
    assert config.VIDEO_PROVIDER == "mock", "the default really is different"


def test_fingerprint_separates_providers():
    """A clip generated by mock must not be reused for a kaggle render."""
    spec = SceneSpec(scene_id="s", index=0, title="t", prompt="p", caption=None,
                     seconds=3, width=1080, height=1920)
    assert render.scene_fingerprint(spec, "kaggle") != render.scene_fingerprint(spec, "mock")
    assert render.scene_fingerprint(spec, "kaggle") != render.scene_fingerprint(spec, "ltx")
    assert render.scene_fingerprint(spec, "kaggle") == render.scene_fingerprint(spec, "kaggle")


# --- 15. worker-side pure logic --------------------------------------------

def test_worker_frame_counts_are_model_legal():
    """LTX-Video needs 8k+1 frames; anything else is not a valid request."""
    for seconds in [0.5, 1, 2, 3, 5, 7, 10, 15, 20]:
        frames = worker.frame_count(seconds, fps=25)
        assert (frames - 1) % 8 == 0, (seconds, frames)
        # Must cover the request so the backend can trim, never fall short.
        assert frames >= seconds * 25, (seconds, frames)
        assert frames > 1


def test_worker_has_no_six_second_billing_floor():
    """Unlike the hosted API, a 2-second scene generates about 2 seconds."""
    short = worker.frame_count(2, fps=25)
    assert short / 25 < 3.0, f"{short} frames is far more than 2 seconds"
    assert worker.frame_count(1, fps=25) < worker.frame_count(5, fps=25)


def test_worker_generation_size_is_t4_legal():
    portrait = worker.generation_size(1080, 1920)
    landscape = worker.generation_size(1920, 1080)
    square = worker.generation_size(1080, 1080)
    for w, h in (portrait, landscape, square):
        # LTX-Video requires both dimensions divisible by 32.
        assert w % 32 == 0 and h % 32 == 0, (w, h)
        # And small enough to be plausible on a 16GB T4.
        assert w * h <= 1280 * 1280, (w, h)
    assert portrait[1] > portrait[0], "portrait request must generate portrait"
    assert landscape[0] > landscape[1], "landscape request must generate landscape"


def test_worker_quality_maps_without_exposing_models():
    assert worker.steps_for_quality("standard") == worker.STEPS_STANDARD
    assert worker.steps_for_quality("high") == worker.STEPS_HIGH
    assert worker.steps_for_quality("") == worker.STEPS_STANDARD
    # Distilled checkpoints want few steps; guard against a silly default.
    assert 1 <= worker.STEPS_STANDARD <= 16
    assert 1 <= worker.STEPS_HIGH <= 16


def test_worker_client_refuses_missing_configuration():
    for base, token in [("", "t"), ("https://x", "")]:
        try:
            worker.ReelForgeClient(base, token)
        except RuntimeError as exc:
            assert "not set" in str(exc)
        else:
            raise AssertionError(f"accepted base={base!r} token={bool(token)}")


def test_dry_run_runner_needs_no_model():
    """The loop must be exercisable with no GPU and no weights."""
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    runner = worker.DryRunRunner()
    runner.warm_up()
    out = TMP / "dryrun.mp4"
    runner.generate(
        worker.GenerationRequest(prompt="a courtyard", seconds=2, width=1080,
                                 height=1920, quality="standard"),
        out,
    )
    assert out.exists() and ffmpeg.probe_duration(str(out)) is not None
    assert "no model" in runner.describe()


def test_ltx_runner_fails_clearly_without_a_checkout():
    runner = worker.LTXRunner(repo_dir=str(TMP / "not-there"),
                              pipeline_config="configs/x.yaml")
    try:
        runner.warm_up()
    except RuntimeError as exc:
        assert "not found" in str(exc)
        assert "README" in str(exc)
    else:
        raise AssertionError("a missing LTX checkout was not reported")
    # The configured model is the T4-appropriate 2B distilled checkpoint.
    assert "2b" in worker.LTX_PIPELINE_CONFIG.lower()
    assert "13b" not in worker.LTX_PIPELINE_CONFIG.lower()


# --- existing providers must be untouched ----------------------------------

def test_mock_provider_still_works():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    out = TMP / "mock_still_ok.mp4"
    providers.MockGenerator().generate(
        SceneSpec(scene_id="s", index=0, title="Opening", prompt="p",
                  caption=None, seconds=2, width=1080, height=1920),
        out,
    )
    assert out.exists()
    assert abs((ffmpeg.probe_duration(str(out)) or 0) - 2) < 0.5


def test_hosted_ltx_provider_is_still_intact():
    from app import ltx

    gen = providers.build_generator("ltx")
    assert gen.name == "ltx"
    # Still structurally sound: the planning helpers and limits are present.
    assert ltx.plan_chunks(7, 20) == [(8, 7.0)]
    assert ltx.choose_resolution(1080, 1920) == "1080x1920"
    assert gen.available() is False, "no LTX key is set in tests"


def demo():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} kaggle checks passed")
    if not HAVE_FFMPEG:
        print("NOTE: ffmpeg was not on PATH; video checks were skipped.")


if __name__ == "__main__":
    try:
        demo()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
