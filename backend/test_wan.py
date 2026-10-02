"""Wan 2.1 provider and worker API checks.

Needs no Kaggle account, no GPU, no model weights and no internet — same
approach as test_kaggle.py. The external worker is faked with wan_worker's
own DryRunRunner, calling the same HTTP endpoints a real Kaggle session
would. This exists to prove two things a GPU-less environment still can:
the "wan" queue lane is isolated from the "kaggle" one, and the round trip
through the worker API produces a normalised clip.

    python test_wan.py
"""
import json
import os
import shutil
import subprocess
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
import wan2gp_job  # noqa: E402

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
    diffusers==0.37.1 and leaves the pipeline at fp32; `torch_dtype=` is
    required. That cast also does not reliably reach the transformer, so
    Wan's runner passes an explicit fp16 fixup for it and fp32 for the VAE.
    The shared loader applies those fixups and enables CPU offload. This
    test reads the source rather than calling warm_up(), which needs a GPU
    and the weights.
    """
    root = Path(__file__).resolve().parent.parent / "kaggle"
    worker_source = (root / "wan_worker.py").read_text(encoding="utf-8")
    loader_source = (root / "_local_runner.py").read_text(encoding="utf-8")
    assert 'dtype_fixups={"vae": torch.float32, "transformer": torch.float16}' in worker_source
    assert "torch_dtype=dtype" in loader_source
    assert "dtype=torch.float16" not in loader_source
    assert ".to(cast_dtype)" in loader_source
    assert 'enable_model_cpu_offload(device=f"cuda:{device}")' in loader_source


def test_worker_client_refuses_missing_configuration():
    for base, token in (("", "t"), ("https://x", "")):
        try:
            worker.ReelForgeClient(base, token)
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass


# --- proven Wan2GP job contract (no GPU, no network) -------------------------

PROVEN_PROBE = {
    "codec": "h264", "width": 416, "height": 240,
    "frames": "81", "rate": "16/1", "duration": 81 / 16,
}
WAN2GP_ENV = {
    "REELFORGE_API_BASE": "https://example.test",
    "REELFORGE_WORKER_TOKEN": "test-worker-token-do-not-use",
}


class FakeWan2GPApi:
    def __init__(self, job):
        self.job = job
        self.fails = []
        self.completed = []
        self.claims = 0

    def next_job(self):
        self.claims += 1
        return self.job

    def fail(self, job_id, detail, retryable=False):
        self.fails.append((job_id, detail, retryable))

    def complete(self, job_id, clip):
        self.completed.append((job_id, Path(clip).read_bytes()))
        return {"job_id": job_id, "status": "completed"}


def wan2gp_root(name: str) -> Path:
    root = TMP / name
    preflight = root / "wan2gp_preflight"
    preflight.mkdir(parents=True)
    (preflight / "load_ready.ok").write_text("ok\n", encoding="utf-8")
    (root / "t2v_1_3B_settings.json").write_text(
        json.dumps({"model_type": "template", "prompt": "dry-run placeholder"}) + "\n",
        encoding="utf-8",
    )
    return root


def test_wan2gp_settings_match_the_proven_command():
    settings = wan2gp_job.proven_overrides("ocean room")
    assert settings == {
        "model_type": "t2v_1.3B",
        "prompt": "ocean room",
        "resolution": "416x240",
        "video_length": 81,
        "num_inference_steps": 20,
        "seed": 1,
        "repeat_generation": 1,
        "prompt_enhancer": "",
        "spatial_upsampling": "",
        "temporal_upsampling": "",
        "override_profile": 5,
        "override_attention": "sdpa",
        "image_mode": 0,
    }
    argv = wan2gp_job.generation_argv("python", "/tmp/t2v_1_3B_settings.json", "/tmp/out")
    assert argv == [
        "python", "wgp.py",
        "--process", "/tmp/t2v_1_3B_settings.json",
        "--output-dir", "/tmp/out",
        "--gpu", "cuda:0",
        "--attention", "sdpa",
        "--profile", "5",
        "--fp16",
        "--verbose", "2",
    ]
    env = wan2gp_job.generation_env({}, Path("/repo/ckpts"))
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert abs(wan2gp_job.PROVEN_SECONDS - 5.0625) < 1e-9


def test_wan2gp_ignores_export_frame_quality_and_reference():
    decision = wan2gp_job.decide({
        "seconds": 5,
        "prompt": "ocean room",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "quality": "high",
        "reference_url": "/api/worker/kaggle/jobs/x/reference",
    })
    assert decision["action"] == "generate"
    assert decision["settings"]["prompt"] == "ocean room"
    assert decision["settings"]["resolution"] == "416x240"
    assert decision["settings"]["video_length"] == 81
    assert decision["settings"]["num_inference_steps"] == 20
    assert decision["settings"]["image_mode"] == 0


def test_wan2gp_rejects_any_duration_other_than_five_seconds():
    for seconds in (1, 2, 4, 6, 30):
        decision = wan2gp_job.decide({"seconds": seconds, "prompt": "ocean room"})
        assert decision["action"] == "fail", seconds
        assert decision["retryable"] is False
        assert decision["settings"] is None
        assert "will not change settings" in decision["reason"]
    assert wan2gp_job.decide({"seconds": 5, "prompt": "   "})["action"] == "fail"
    assert wan2gp_job.decide({"seconds": "five", "prompt": "ocean"})["action"] == "fail"
    assert wan2gp_job.validate_probe(PROVEN_PROBE) is None
    short = dict(PROVEN_PROBE, frames="17", duration=1.0625)
    assert wan2gp_job.validate_probe(short) is not None


def test_config_records_the_proven_wan2gp_settings():
    assert config.WAN2GP_COMMIT == wan2gp_job.COMMIT == "b8b18f8114e432eea8f3d7e853a51dd91fa99571"
    assert config.WAN2GP_MODEL_TYPE == "t2v_1.3B"
    assert (config.WAN2GP_WIDTH, config.WAN2GP_HEIGHT) == (416, 240)
    assert (config.WAN2GP_FRAMES, config.WAN2GP_FPS, config.WAN2GP_STEPS) == (81, 16, 20)
    assert config.WAN2GP_SEED == 1
    assert config.WAN2GP_ATTENTION == "sdpa"
    assert config.WAN2GP_PROFILE == "5"
    assert config.WAN2GP_GPU == "cuda:0"
    assert config.WAN2GP_SCENE_SECONDS == 5
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert 'REELFORGE_VIDEO_PROVIDER", "mock"' in source
    assert 'REELFORGE_WAN_GEN_WIDTH", "416"' in source
    assert 'REELFORGE_WAN_GEN_HEIGHT", "240"' in source
    assert 'REELFORGE_WAN_GEN_FRAMES", "81"' in source
    assert 'REELFORGE_WAN_STEPS_STANDARD", "20"' in source
    assert 'REELFORGE_WAN_STEPS_HIGH", "20"' in source
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "REELFORGE_WAN_GEN_WIDTH=416" in example
    assert "REELFORGE_WAN_GEN_HEIGHT=240" in example
    assert "REELFORGE_WAN_GEN_FRAMES=81" in example
    assert "REELFORGE_WAN_STEPS_STANDARD=20" in example
    assert "REELFORGE_WAN_STEPS_HIGH=20" in example


def test_polling_notebook_embeds_the_job_module_and_proven_flags():
    root = Path(__file__).resolve().parent.parent / "kaggle"
    nb = json.loads((root / "wan2gp_poll.ipynb").read_text(encoding="utf-8"))
    joined = "\n".join("".join(cell["source"]) for cell in nb["cells"])
    module = (root / "wan2gp_job.py").read_text(encoding="utf-8")
    assert module.strip() in joined
    assert "run_kaggle_session()" in joined
    assert "b8b18f8114e432eea8f3d7e853a51dd91fa99571" in joined
    assert '"--fp16"' in joined
    assert '"--attention", "sdpa"' in joined
    assert '"--profile", "5"' in joined
    assert '"--gpu", "cuda:0"' in joined
    assert "/api/worker/kaggle/jobs/next" in joined
    assert "notebook305a99d5b4" not in joined


def _stop(exc: SystemExit) -> str:
    return str(exc.code if exc.code is not None else exc)


def test_claimed_prompt_is_what_generation_receives():
    root = wan2gp_root("prompt-transfer")
    api = FakeWan2GPApi({
        "job_id": "job-ocean",
        "prompt": "forward dolly ocean room",
        "seconds": 5,
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "quality": "high",
    })
    seen = {}

    def generate(repo, settings_path, output_dir, log_path):
        seen["settings"] = json.loads(settings_path.read_text(encoding="utf-8"))
        seen["calls"] = seen.get("calls", 0) + 1
        (output_dir / "clip.mp4").write_bytes(b"fake-mp4")
        return 0

    result = wan2gp_job.run_kaggle_session(
        root=root, environ=WAN2GP_ENV, client=api, generate=generate,
        probe=lambda path: dict(PROVEN_PROBE),
    )
    assert result["prompt"] == "forward dolly ocean room"
    assert api.claims == 1 and seen["calls"] == 1
    assert seen["settings"]["prompt"] == "forward dolly ocean room"
    assert seen["settings"]["video_length"] == 81
    assert seen["settings"]["resolution"] == "416x240"
    assert seen["settings"]["num_inference_steps"] == 20
    assert seen["settings"]["seed"] == 1
    assert len(api.completed) == 1 and api.completed[0][1] == b"fake-mp4"
    assert api.fails == []


def test_failed_generation_does_not_rewrite_settings():
    root = wan2gp_root("gen-fail")
    api = FakeWan2GPApi({
        "job_id": "job-fail", "prompt": "ocean room", "seconds": 5,
    })

    def generate(repo, settings_path, output_dir, log_path):
        return 7

    try:
        wan2gp_job.run_kaggle_session(
            root=root, environ=WAN2GP_ENV, client=api, generate=generate,
            probe=lambda path: dict(PROVEN_PROBE),
        )
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "exited 7" in _stop(exc)
    written = json.loads((root / "t2v_1_3B_settings.json").read_text(encoding="utf-8"))
    assert written["prompt"] == "ocean room"
    assert written["resolution"] == "416x240"
    assert written["video_length"] == 81
    assert written["num_inference_steps"] == 20
    assert written["override_attention"] == "sdpa"
    assert written["override_profile"] == 5
    assert api.fails == [("job-fail", "wgp.py exited 7. Settings were not changed.", False)]
    assert api.completed == []


def test_wrong_duration_fails_before_generation():
    root = wan2gp_root("too-long")
    before = (root / "t2v_1_3B_settings.json").read_text(encoding="utf-8")
    api = FakeWan2GPApi({
        "job_id": "job-6", "prompt": "ocean room", "seconds": 6,
        "width": 1080, "height": 1920, "fps": 30,
    })
    calls = {"n": 0}

    def generate(*args):
        calls["n"] += 1
        return 0

    try:
        wan2gp_job.run_kaggle_session(
            root=root, environ=WAN2GP_ENV, client=api, generate=generate,
            probe=lambda path: dict(PROVEN_PROBE),
        )
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "will not change settings" in _stop(exc)
    assert calls["n"] == 0
    assert (root / "t2v_1_3B_settings.json").read_text(encoding="utf-8") == before
    assert api.completed == []
    assert api.fails[0][0] == "job-6" and api.fails[0][2] is False


def test_probe_mismatch_is_not_uploaded():
    root = wan2gp_root("bad-probe")
    api = FakeWan2GPApi({"job_id": "job-probe", "prompt": "ocean room", "seconds": 5})

    def generate(repo, settings_path, output_dir, log_path):
        (output_dir / "clip.mp4").write_bytes(b"not-really")
        return 0

    bad = dict(PROVEN_PROBE, frames="17", duration=1.0625)
    try:
        wan2gp_job.run_kaggle_session(
            root=root, environ=WAN2GP_ENV, client=api, generate=generate,
            probe=lambda path: bad,
        )
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "not uploaded" in _stop(exc)
    assert api.completed == []
    assert api.fails[0][2] is False


def test_existing_output_does_not_claim_or_generate():
    root = wan2gp_root("already-generated")
    (root / "out").mkdir()
    (root / "out" / "clip.mp4").write_bytes(b"already")
    api = FakeWan2GPApi({
        "job_id": "job-again", "prompt": "ocean room", "seconds": 5,
    })
    calls = {"n": 0}

    def generate(*args):
        calls["n"] += 1
        return 0

    try:
        wan2gp_job.run_kaggle_session(
            root=root, environ=WAN2GP_ENV, client=api, generate=generate,
        )
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "Nothing was claimed" in _stop(exc)
    assert api.claims == 0 and calls["n"] == 0 and api.fails == [] and api.completed == []


def test_no_pending_job_does_not_generate():
    root = wan2gp_root("idle")
    api = FakeWan2GPApi(None)
    calls = {"n": 0}

    def generate(*args):
        calls["n"] += 1
        return 0

    try:
        wan2gp_job.run_kaggle_session(
            root=root, environ=WAN2GP_ENV, client=api, generate=generate,
        )
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "No pending wan job" in _stop(exc)
    assert calls["n"] == 0 and api.fails == [] and api.completed == []


def test_worker_api_claim_returns_the_scene_prompt():
    project = make_project()
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=first_scene_id(project),
            provider="wan", prompt="forward dolly ocean room", duration=5,
            width=1080, height=1920, fps=30, quality="high",
        )
    res = client.get(
        "/api/worker/kaggle/jobs/next",
        params={"worker_id": "wan2gp-1", "provider": "wan"}, headers=AUTH,
    )
    assert res.status_code == 200, res.text
    job = res.json()["job"]
    assert job["job_id"] == job_id
    assert job["prompt"] == "forward dolly ocean room"
    assert job["seconds"] == 5
    decision = wan2gp_job.decide(job)
    assert decision["action"] == "generate"
    assert decision["settings"]["prompt"] == "forward dolly ocean room"
    assert decision["settings"]["resolution"] == "416x240"


def _ffmpeg_clip(dest: Path, frames: int) -> bytes:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=s=416x240:r=16",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest),
        ],
        check=True,
    )
    return dest.read_bytes()


def test_mp4_upload_completes_a_five_second_job():
    if not HAVE_FFMPEG:
        return
    project = make_project()
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=first_scene_id(project),
            provider="wan", prompt="ocean room", duration=5,
            width=1080, height=1920, fps=30, quality="standard",
        )
    claimed = client.get(
        "/api/worker/kaggle/jobs/next",
        params={"worker_id": "wan2gp-1", "provider": "wan"}, headers=AUTH,
    )
    assert claimed.json()["job"]["job_id"] == job_id
    payload, content_type = wan2gp_job.multipart_payload(
        "result_wan2gp.mp4", _ffmpeg_clip(TMP / "five.mp4", 81),
    )
    res = client.post(
        f"/api/worker/kaggle/jobs/{job_id}/complete",
        content=payload,
        headers={**AUTH, "Content-Type": content_type},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"job_id": job_id, "status": "completed"}
    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_COMPLETED


def test_invalid_uploads_are_rejected_and_the_job_can_be_failed():
    project = make_project()
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=first_scene_id(project),
            provider="wan", prompt="ocean room", duration=5,
            width=1080, height=1920, fps=30, quality="standard",
        )
    claimed = client.get(
        "/api/worker/kaggle/jobs/next",
        params={"worker_id": "wan2gp-1", "provider": "wan"}, headers=AUTH,
    )
    assert claimed.json()["job"]["job_id"] == job_id

    empty = client.post(
        f"/api/worker/kaggle/jobs/{job_id}/complete",
        files={"file": ("scene.mp4", b"", "video/mp4")}, headers=AUTH,
    )
    assert empty.status_code == 422

    garbage = client.post(
        f"/api/worker/kaggle/jobs/{job_id}/complete",
        files={"file": ("scene.mp4", b"not a video", "video/mp4")}, headers=AUTH,
    )
    assert garbage.status_code == 422

    if HAVE_FFMPEG:
        short_bytes = _ffmpeg_clip(TMP / "one-second.mp4", 16)
        short = client.post(
            f"/api/worker/kaggle/jobs/{job_id}/complete",
            files={"file": ("scene.mp4", short_bytes, "video/mp4")}, headers=AUTH,
        )
        assert short.status_code == 422, short.text
        assert "needs 5s" in short.text

    with db.connect() as conn:
        assert repo.get_scene_job(conn, job_id)["status"] == repo.JOB_CLAIMED

    failed = client.post(
        f"/api/worker/kaggle/jobs/{job_id}/fail",
        json=wan2gp_job.failure_payload("probe mismatch; settings were not changed", False),
        headers=AUTH,
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == repo.JOB_FAILED
    with db.connect() as conn:
        row = repo.get_scene_job(conn, job_id)
    assert row["status"] == repo.JOB_FAILED
    assert row["error"] == "This scene couldn’t be generated. Please try again."
    assert "probe mismatch" not in (row["error"] or "")


def test_six_second_scene_fails_without_requeue():
    project = make_project()
    with db.connect() as conn:
        job_id = repo.create_scene_job(
            conn, project_id=project["id"], scene_id=first_scene_id(project),
            provider="wan", prompt="ocean room", duration=6,
            width=1080, height=1920, fps=30, quality="standard",
        )
    job = client.get(
        "/api/worker/kaggle/jobs/next",
        params={"worker_id": "wan2gp-1", "provider": "wan"}, headers=AUTH,
    ).json()["job"]
    decision = wan2gp_job.decide(job)
    assert decision["action"] == "fail"
    res = client.post(
        f"/api/worker/kaggle/jobs/{job_id}/fail",
        json=wan2gp_job.failure_payload(decision["reason"], decision["retryable"]),
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "failed"
    with db.connect() as conn:
        assert repo.get_scene_job(conn, job_id)["status"] == repo.JOB_FAILED


def test_five_second_scene_round_trip_normalises_to_the_storyboard():
    if not HAVE_FFMPEG:
        return
    import threading

    project = make_project()
    spec = SceneSpec(
        scene_id=first_scene_id(project), index=0, title="Scene",
        prompt="forward dolly ocean room", caption=None, seconds=5,
        width=1080, height=1920, fps=30,
    )
    out = TMP / "normalised.mp4"
    errors = []

    def fake_worker():
        try:
            res = client.get(
                "/api/worker/kaggle/jobs/next",
                params={"worker_id": "wan2gp-1", "provider": "wan"}, headers=AUTH,
            )
            job = res.json()["job"]
            while job is None:
                res = client.get(
                    "/api/worker/kaggle/jobs/next",
                    params={"worker_id": "wan2gp-1", "provider": "wan"}, headers=AUTH,
                )
                job = res.json()["job"]
            decision = wan2gp_job.decide(job)
            assert decision["action"] == "generate", decision
            assert decision["settings"]["prompt"] == "forward dolly ocean room"
            assert decision["settings"]["video_length"] == 81
            payload, content_type = wan2gp_job.multipart_payload(
                "result_wan2gp.mp4", _ffmpeg_clip(TMP / "round.mp4", 81),
            )
            uploaded = client.post(
                f"/api/worker/kaggle/jobs/{job['job_id']}/complete",
                content=payload, headers={**AUTH, "Content-Type": content_type},
            )
            assert uploaded.status_code == 200, uploaded.text
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=fake_worker, daemon=True)
    thread.start()
    providers.build_generator("wan").generate(spec, out)
    thread.join(timeout=20)
    assert not errors, errors
    assert out.exists() and out.stat().st_size > 0
    duration = ffmpeg.probe_duration(str(out))
    assert duration is not None and abs(duration - 5.0) < 0.15, duration
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert info == "1080,1920,30/1", info


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
