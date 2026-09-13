"""Wan 2.1 provider and worker API checks.

Needs no Kaggle account, no GPU, no model weights and no internet — same
approach as test_kaggle.py. The external worker is faked with wan_worker's
own DryRunRunner, calling the same HTTP endpoints a real Kaggle session
would. This exists to prove two things a GPU-less environment still can:
the "wan" queue lane is isolated from the "kaggle" one, and the round trip
through the worker API produces a normalised clip.

    python test_wan.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="reelforge-wan-")
os.environ["REELFORGE_DATA_DIR"] = _TMP
os.environ["REELFORGE_JOB_RUNNER"] = "external"
os.environ["REELFORGE_VIDEO_PROVIDER"] = "mock"
os.environ["REELFORGE_KAGGLE_WORKER_TOKEN"] = "test-worker-token-do-not-use"
os.environ["REELFORGE_KAGGLE_POLL_SECONDS"] = "0.05"
os.environ["REELFORGE_WAN_JOB_TIMEOUT"] = "6"
os.environ["REELFORGE_KAGGLE_CLAIM_TIMEOUT"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, ffmpeg, providers, repo  # noqa: E402
from app.providers import SceneSpec  # noqa: E402
from app.main import app  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kaggle"))
import wan_worker as worker  # noqa: E402

client = TestClient(app)
TMP = Path(_TMP)
TOKEN = config.KAGGLE_WORKER_TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HAVE_FFMPEG = ffmpeg.ffmpeg_available()


def make_project(**overrides):
    body = {"idea": "a quiet courtyard at dusk", "category": "Real Estate",
            "duration": 12, "aspect_ratio": "9:16"}
    body.update(overrides)
    res = client.post("/api/projects", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def first_scene_id(project: dict) -> str:
    res = client.get(f"/api/projects/{project['id']}")
    assert res.status_code == 200
    return res.json()["scenes"][0]["id"]


# --- provider registration and availability ---------------------------------

def test_wan_is_registered_alongside_the_others():
    assert providers.PROVIDER_NAMES == ("mock", "ltx", "kaggle", "wan")


def test_wan_available_only_with_a_worker_token():
    gen = providers.build_generator("wan")
    assert gen.name == "wan"
    assert gen.available() is True  # token set at module import time above


def test_wan_unavailable_without_worker_token():
    original = config.KAGGLE_WORKER_TOKEN
    config.KAGGLE_WORKER_TOKEN = ""
    try:
        gen = providers.build_generator("wan")
        assert gen.available() is False
        assert "REELFORGE_KAGGLE_WORKER_TOKEN" in gen.configuration_error()
    finally:
        config.KAGGLE_WORKER_TOKEN = original


def test_health_exposes_wan():
    res = client.get("/health")
    assert res.status_code == 200
    assert "wan" in res.json()["providers"]


# --- queue lane isolation: the one genuinely new behaviour -------------------

def test_wan_and_kaggle_queues_do_not_cross_claim():
    project = make_project()
    scene_id = first_scene_id(project)
    with db.connect() as conn:
        kaggle_job = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene_id, provider="kaggle",
            prompt="a", duration=2, width=1080, height=1920, fps=25, quality="standard",
        )
        wan_job = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene_id, provider="wan",
            prompt="b", duration=2, width=1080, height=1920, fps=16, quality="standard",
        )

    res = client.get("/api/worker/kaggle/jobs/next",
                      params={"worker_id": "w", "provider": "wan"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["job"]["job_id"] == wan_job

    res = client.get("/api/worker/kaggle/jobs/next",
                      params={"worker_id": "w", "provider": "kaggle"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json()["job"]["job_id"] == kaggle_job


def test_unknown_worker_provider_is_rejected():
    res = client.get("/api/worker/kaggle/jobs/next",
                      params={"worker_id": "w", "provider": "not-a-thing"},
                      headers=AUTH)
    assert res.status_code == 422


# --- claim-timeout regression: a real bug found generating a real clip ------
#
# A live 5s/30-step Wan generation on a T4 took ~2124s. The stale-claim sweep
# used one shared timeout (KAGGLE_CLAIM_TIMEOUT, 600s default, tuned for LTX's
# much faster distilled path) for every provider, so it requeued the Wan job
# out from under the still-running worker, and its eventual upload would have
# been rejected as no longer claimed. Fixed by scoping the sweep to one
# provider per call, each using its own timeout.

def test_kaggle_sweep_never_touches_a_wan_job():
    project = make_project()
    scene_id = first_scene_id(project)
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene_id, provider="wan",
            prompt="a", duration=5, width=1080, height=1920, fps=16, quality="standard",
        )
        conn.execute(
            db.sql("UPDATE scene_jobs SET status = ?, claimed_by = ?,"
                   " claimed_at = ? WHERE id = ?"),
            (repo.JOB_CLAIMED, "wan-1", "2000-01-01 00:00:00", job_id),
        )
        # However short, a sweep scoped to "kaggle" must never touch this
        # job: it belongs to a different provider's queue lane entirely.
        repo.requeue_stale_scene_jobs(
            conn, provider="kaggle", claim_timeout_seconds=1, max_attempts=5,
        )
        row = repo.get_scene_job(conn, job_id)
        # Tests share one job queue; leaving this claimed would let a later
        # test's poll pick up this ancient job instead of its own.
        conn.execute(db.sql("UPDATE scene_jobs SET status = ? WHERE id = ?"),
                     (repo.JOB_CANCELLED, job_id))
    assert row["status"] == repo.JOB_CLAIMED


def test_wan_sweep_uses_wans_own_long_timeout():
    from datetime import datetime, timedelta, timezone

    project = make_project()
    scene_id = first_scene_id(project)
    # Older than the old shared 600s default, but well inside Wan's own
    # 3600s one: exactly the still-generating job the bug used to requeue.
    claimed_700s_ago = (
        datetime.now(timezone.utc) - timedelta(seconds=700)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene_id, provider="wan",
            prompt="a", duration=5, width=1080, height=1920, fps=16, quality="standard",
        )
        conn.execute(
            db.sql("UPDATE scene_jobs SET status = ?, claimed_by = ?,"
                   " claimed_at = ? WHERE id = ?"),
            (repo.JOB_CLAIMED, "wan-1", claimed_700s_ago, job_id),
        )
        repo.requeue_stale_scene_jobs(
            conn, provider="wan",
            claim_timeout_seconds=config.WAN_CLAIM_TIMEOUT_SECONDS,
            max_attempts=5,
        )
        row = repo.get_scene_job(conn, job_id)
        conn.execute(db.sql("UPDATE scene_jobs SET status = ? WHERE id = ?"),
                     (repo.JOB_CANCELLED, job_id))
    assert row["status"] == repo.JOB_CLAIMED, (
        "a still-generating Wan job was requeued before its own timeout elapsed"
    )


def test_wan_sweep_still_rescues_a_genuinely_dead_worker():
    project = make_project()
    scene_id = first_scene_id(project)
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=scene_id, provider="wan",
            prompt="a", duration=5, width=1080, height=1920, fps=16, quality="standard",
        )
        conn.execute(
            db.sql("UPDATE scene_jobs SET status = ?, claimed_by = ?,"
                   " claimed_at = ? WHERE id = ?"),
            (repo.JOB_CLAIMED, "wan-1", "2000-01-01 00:00:00", job_id),
        )
        result = repo.requeue_stale_scene_jobs(
            conn, provider="wan", claim_timeout_seconds=1, max_attempts=5,
        )
        row = repo.get_scene_job(conn, job_id)
        conn.execute(db.sql("UPDATE scene_jobs SET status = ? WHERE id = ?"),
                     (repo.JOB_CANCELLED, job_id))
    assert result["requeued"] >= 1
    assert row["status"] == repo.JOB_PENDING
    assert row["claimed_by"] is None and row["claimed_at"] is None


# --- end-to-end dry-run round trip -------------------------------------------

def test_dry_run_round_trip_normalises_the_worker_clip():
    if not HAVE_FFMPEG:
        return
    project = make_project()
    scene_id = first_scene_id(project)
    spec = SceneSpec(
        scene_id=scene_id, index=0, title="Scene", prompt="a courtyard",
        caption=None, seconds=2, width=1080, height=1920, fps=25,
    )
    generator = providers.build_generator("wan")
    out = TMP / "final.mp4"

    import threading

    def fake_worker():
        res = client.get("/api/worker/kaggle/jobs/next",
                          params={"worker_id": "wan-1", "provider": "wan"},
                          headers=AUTH)
        job = res.json()["job"]
        while job is None:
            res = client.get("/api/worker/kaggle/jobs/next",
                              params={"worker_id": "wan-1", "provider": "wan"},
                              headers=AUTH)
            job = res.json()["job"]
        runner = worker.DryRunRunner()
        clip = TMP / "worker-clip.mp4"
        runner.generate(
            worker.GenerationRequest(
                prompt=job["prompt"], seconds=float(job["seconds"]),
                width=int(job["width"]), height=int(job["height"]),
                quality=job["quality"],
            ),
            clip,
        )
        with open(clip, "rb") as fh:
            client.post(f"/api/worker/kaggle/jobs/{job['job_id']}/complete",
                        files={"file": ("scene.mp4", fh, "video/mp4")},
                        headers=AUTH)

    thread = threading.Thread(target=fake_worker, daemon=True)
    thread.start()
    generator.generate(spec, out)
    thread.join(timeout=5)

    assert out.exists() and out.stat().st_size > 0
    duration = ffmpeg.probe_duration(str(out))
    assert duration is not None and abs(duration - 2.0) < 0.3


# --- pure helper functions ----------------------------------------------------

def test_worker_frame_counts_are_model_legal():
    for seconds in (0.5, 1, 2, 3.7, 10):
        frames = worker.frame_count(seconds, fps=16)
        assert (frames - 1) % worker.FRAME_QUANTUM == 0
        assert frames >= 1


def test_worker_generation_size_is_t4_legal():
    portrait = worker.generation_size(1080, 1920)
    landscape = worker.generation_size(1920, 1080)
    for w, h in (portrait, landscape):
        assert w % 16 == 0 and h % 16 == 0


def test_worker_quality_maps_without_exposing_models():
    assert worker.steps_for_quality("standard") == worker.STEPS_STANDARD
    assert worker.steps_for_quality("high") == worker.STEPS_HIGH
    assert worker.steps_for_quality("") == worker.STEPS_STANDARD


def test_dry_run_runner_needs_no_model():
    if not HAVE_FFMPEG:
        return
    out = TMP / "dry.mp4"
    runner = worker.DryRunRunner()
    runner.warm_up()
    runner.generate(
        worker.GenerationRequest(prompt="a courtyard", seconds=2, width=1080,
                                  height=1920, quality="standard"),
        out,
    )
    assert out.exists() and out.stat().st_size > 0


def test_worker_uses_the_verified_wan_load_sequence():
    """Regression guard for a bug confirmed on real Kaggle hardware.

    `dtype=` is silently ignored by WanPipeline.from_pretrained on
    diffusers==0.37.1 (no error, no warning that survives to the log level
    checked) and leaves everything at fp32; `torch_dtype=` is required. It
    also does not reliably reach the transformer submodule, which needs an
    explicit `.to(torch.float16)` afterward. Both were verified by generating
    a real clip on a T4. This test reads the source rather than exercising
    warm_up() itself, since that needs a GPU, ~29GB of weights and network
    access this suite deliberately does not depend on.
    """
    source = (Path(__file__).resolve().parent.parent / "kaggle" / "wan_worker.py").read_text()
    assert "torch_dtype=torch.float16" in source
    assert "dtype=torch.float16" not in source.replace("torch_dtype=torch.float16", "")
    assert "pipeline.transformer.to(torch.float16)" in source
    assert "pipeline.vae.to(torch.float32)" in source
    assert 'enable_model_cpu_offload(device="cuda:0")' in source


def test_worker_client_refuses_missing_configuration():
    for base, token in (("", "t"), ("https://x", "")):
        try:
            worker.ReelForgeClient(base, token)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass


def demo():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} wan checks passed")
    if not HAVE_FFMPEG:
        print("NOTE: ffmpeg was not on PATH; video checks were skipped.")


if __name__ == "__main__":
    try:
        demo()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
