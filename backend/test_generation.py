"""Generation-provider checks: selection, chunking, aspect ratio, failure,
retry and scene isolation.

IMPORTANT ABOUT SCOPE
---------------------
Nothing here proves that real LTX generation works. The LTX tests use an
injected fake HTTP transport, so they verify *our* request construction,
polling, error mapping and post-processing — not the model, the service, or
our credentials. Tests that need the real API live in
test_ltx_integration.py and skip themselves without credentials.

    python test_generation.py
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="reelforge-gen-")
os.environ["REELFORGE_DATA_DIR"] = _TMP
os.environ["REELFORGE_JOB_RUNNER"] = "external"

from app import config, ffmpeg, ltx, providers, render  # noqa: E402
from app.ffmpeg import RenderError  # noqa: E402
from app.providers import SceneSpec  # noqa: E402

TMP = Path(_TMP)
HAVE_FFMPEG = ffmpeg.ffmpeg_available()


# --- fakes ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeLTXService:
    """Stands in for api.ltx.io. Records what we sent it."""

    def __init__(self, clip: Path, fail_status: str | None = None):
        self.clip = clip
        self.fail_status = fail_status
        self.submissions: list[dict] = []
        self.polls = 0

    def __call__(self, req, timeout=None):
        url = req.full_url
        if req.method == "POST":
            body = json.loads(req.data.decode())
            # The endpoint is part of the path, so record it alongside the body.
            body["_endpoint"] = url.rsplit("/", 1)[-1]
            body["_auth"] = req.get_header("Authorization")
            self.submissions.append(body)
            return FakeResponse(json.dumps({"id": f"job-{len(self.submissions)}"}).encode())
        if url.endswith(".mp4"):
            return FakeResponse(self.clip.read_bytes())
        self.polls += 1
        if self.fail_status:
            return FakeResponse(json.dumps({
                "status": self.fail_status,
                "error": {"message": "internal detail that must not leak"},
            }).encode())
        return FakeResponse(json.dumps({
            "status": "completed",
            "result": {"video_url": "https://example.invalid/out.mp4"},
        }).encode())


class RecordingHooks:
    """render.RenderHooks that records calls and can serve cached clips."""

    def __init__(self, cache: dict[str, Path] | None = None, cancel_after: int | None = None):
        self.cache = cache or {}
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.logs: list[tuple[str, str]] = []
        self.reused: list[str] = []
        self.cancel_after = cancel_after
        self._checks = 0

    def should_cancel(self):
        self._checks += 1
        return self.cancel_after is not None and self._checks > self.cancel_after

    def cached_clip(self, scene_id, fingerprint):
        hit = self.cache.get(scene_id)
        if hit is not None:
            self.reused.append(scene_id)
        return hit

    def clip_succeeded(self, scene_id, fingerprint, clip):
        self.succeeded.append(scene_id)
        kept = TMP / f"cached_{scene_id}.mp4"
        shutil.copy2(clip, kept)
        self.cache[scene_id] = kept

    def clip_failed(self, scene_id, fingerprint, message):
        self.failed.append((scene_id, message))

    def generation_logged(self, scene_id, status, seconds, elapsed_ms, error):
        self.logs.append((scene_id, status))


class CountingGenerator:
    """Mock-like generator that counts calls and can fail chosen scenes."""

    name = "mock"

    def __init__(self, fail_indices=(), clip_source: Path | None = None):
        self.fail_indices = set(fail_indices)
        self.calls: list[str] = []
        self.clip_source = clip_source

    def available(self):
        return True

    def generate(self, spec, out):
        self.calls.append(spec.scene_id)
        if spec.index in self.fail_indices:
            raise RenderError("the video service could not generate this scene")
        if self.clip_source:
            shutil.copy2(self.clip_source, out)
        else:
            ffmpeg.run(ffmpeg.build_card_clip_cmd(
                out=str(out), seconds=spec.seconds, width=spec.width,
                height=spec.height, caption=spec.title, index=spec.index,
            ))


def make_clip(seconds=6, width=1080, height=1920, fps=25) -> Path:
    """A real mp4 standing in for model output, at LTX-like fps."""
    dest = TMP / f"src_{seconds}_{width}x{height}_{fps}.mp4"
    if not dest.exists():
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", f"testsrc2=s={width}x{height}:d={seconds}:r={fps}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest),
        ], check=True)
    return dest


def scenes_for(durations) -> list[dict]:
    return [
        {"id": f"scene_{i}", "position": i, "title": f"Scene {i + 1}",
         "prompt": f"prompt {i}", "duration": d, "caption": None}
        for i, d in enumerate(durations)
    ]


# --- 1. provider selection --------------------------------------------------

def test_provider_selection():
    assert providers.build_generator("mock").name == "mock"
    assert providers.build_generator("ltx").name == "ltx"
    # The default comes from configuration, and must stay mock for local dev.
    assert providers.build_generator().name == config.VIDEO_PROVIDER
    assert config.VIDEO_PROVIDER == "mock", "mock must remain the default"
    assert set(providers.PROVIDER_NAMES) == {"mock", "ltx"}


# --- 5. invalid provider ----------------------------------------------------

def test_invalid_provider_is_rejected():
    for bad in ("sora", "LTX-2", "mockk", "ltx-video"):
        try:
            providers.build_generator(bad)
        except RenderError as exc:
            assert "unknown video provider" in str(exc), str(exc)
        else:
            raise AssertionError(f"provider {bad!r} was accepted")


def test_unspecified_provider_falls_back_to_the_default():
    # None and "" both mean "not specified", which is how the API passes an
    # absent provider. They must resolve to the configured default, not error.
    assert providers.build_generator(None).name == config.VIDEO_PROVIDER
    assert providers.build_generator("").name == config.VIDEO_PROVIDER


# --- 2. mock provider still works ------------------------------------------

def test_mock_provider_still_renders():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    out = TMP / "mock_scene.mp4"
    spec = SceneSpec(
        scene_id="s1", index=0, title="Opening", prompt="p", caption=None,
        seconds=4, width=1080, height=1920,
    )
    providers.MockGenerator().generate(spec, out)
    assert out.exists() and out.stat().st_size > 0
    duration = ffmpeg.probe_duration(str(out))
    assert duration is not None and abs(duration - 4) < 0.6, duration
    assert providers.MockGenerator().available() is True


def test_mock_needs_no_credentials_or_network():
    # The whole point of keeping mock: local dev and CI need neither.
    assert providers.configuration_error("mock") is None
    for var in ("REELFORGE_LTX_API_KEY", "REELFORGE_LTX_ENDPOINT"):
        assert not os.environ.get(var), f"{var} must not be needed for mock"


# --- 3 & 4. LTX configuration validation -----------------------------------

def test_ltx_reports_missing_configuration():
    no_key = ltx.LTXGenerator(endpoint="https://api.ltx.io", api_key="")
    assert no_key.available() is False
    assert "API_KEY" in (no_key.configuration_error() or "")

    no_endpoint = ltx.LTXGenerator(endpoint="", api_key="k")
    assert no_endpoint.available() is False
    assert "ENDPOINT" in (no_endpoint.configuration_error() or "")

    configured = ltx.LTXGenerator(endpoint="https://api.ltx.io", api_key="k")
    assert configured.available() is True
    assert configured.configuration_error() is None


def test_ltx_refuses_to_generate_without_configuration():
    spec = SceneSpec(
        scene_id="s1", index=0, title="t", prompt="p", caption=None,
        seconds=6, width=1080, height=1920,
    )
    try:
        ltx.LTXGenerator(endpoint="https://api.ltx.io", api_key="").generate(
            spec, TMP / "never.mp4"
        )
    except RenderError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("LTX generated without an API key")


def test_provider_registry_reflects_real_environment():
    # With no key in the environment, ltx must advertise itself unavailable.
    assert providers.available_providers()["mock"] is True
    assert providers.available_providers()["ltx"] is False
    assert "API_KEY" in (providers.configuration_error("ltx") or "")


# --- 9. duration and chunking ----------------------------------------------

def test_duration_quantization():
    # The API accepts only even integers from 6 up, so 5 and 7 must round up.
    assert ltx.quantize_duration(5, 20) == 6
    assert ltx.quantize_duration(6, 20) == 6
    assert ltx.quantize_duration(7, 20) == 8
    assert ltx.quantize_duration(1, 20) == 6
    assert ltx.quantize_duration(9, 20) == 10
    # Never above the model ceiling.
    assert ltx.quantize_duration(20, 10) == 10


def test_chunk_plan_covers_exact_scene_length():
    for max_clip in (10, 20):
        for seconds in range(1, 61):
            try:
                plan = ltx.plan_chunks(seconds, max_clip)
            except RenderError:
                # Only allowed when the cost guard trips.
                assert seconds / max_clip > config.LTX_MAX_CHUNKS_PER_SCENE
                continue
            keeps = sum(keep for _, keep in plan)
            assert keeps == seconds, (seconds, max_clip, plan)
            for generate, keep in plan:
                assert generate >= keep, (generate, keep)
                assert generate in ltx.allowed_durations(max_clip), generate
                assert generate % 2 == 0 and generate >= ltx.MIN_CLIP_SECONDS


def test_chunk_plan_examples_from_the_spec():
    # A 7-second scene: one 8-second generation trimmed to 7.
    assert ltx.plan_chunks(7, 20) == [(8, 7.0)]
    # A 5-second scene: the model's 6-second floor, trimmed back to 5.
    assert ltx.plan_chunks(5, 20) == [(6, 5.0)]
    # 15 seconds on a 10-second model: two chunks, exact total.
    plan = ltx.plan_chunks(15, 10)
    assert len(plan) == 2 and sum(k for _, k in plan) == 15, plan
    # 30 seconds on a 10-second model: three chunks.
    plan30 = ltx.plan_chunks(30, 10)
    assert len(plan30) == 3 and sum(k for _, k in plan30) == 30, plan30


def test_chunk_plan_has_a_cost_guard():
    try:
        ltx.plan_chunks(600, 10)
    except RenderError as exc:
        assert "above the configured limit" in str(exc)
    else:
        raise AssertionError("an unbounded number of billable chunks was allowed")


def test_model_and_limits_mapping():
    # Customer-facing quality maps to models internally; no model names leak.
    assert ltx.model_for_quality("standard") == config.LTX_MODEL_STANDARD
    assert ltx.model_for_quality("high") == config.LTX_MODEL_HIGH
    assert ltx.model_for_quality("") == config.LTX_MODEL_STANDARD
    # pro's documented ceiling is lower, which must drive more chunks.
    assert ltx.limits_for("ltx-2-5-pro").max_clip_seconds == 10
    assert ltx.limits_for("ltx-2-5-fast").max_clip_seconds == 20
    assert ltx.limits_for("something-new").max_clip_seconds == ltx.FALLBACK_MAX_CLIP_SECONDS
    assert len(ltx.plan_chunks(20, 10)) > len(ltx.plan_chunks(20, 20))


# --- 10. aspect ratio handling ---------------------------------------------

def test_aspect_ratio_maps_to_a_supported_generation_resolution():
    # The API offers only 16:9 and 9:16.
    assert ltx.choose_resolution(1080, 1920) == "1080x1920"
    assert ltx.choose_resolution(1920, 1080) == "1920x1080"
    # 1:1 and 4:5 are unsupported upstream, so they generate portrait.
    assert ltx.choose_resolution(1080, 1080) == "1080x1920"
    assert ltx.choose_resolution(1080, 1350) == "1080x1920"
    assert ltx.choose_resolution(1280, 720, "720p") == "1280x720"
    # An unknown tier must not crash a render.
    assert ltx.choose_resolution(1080, 1920, "nonsense") == "1080x1920"


def test_normalize_forces_exact_frame_and_length():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    # Model output at an unsupported ratio and frame rate for our pipeline.
    src = make_clip(seconds=6, width=1080, height=1920, fps=25)
    out = TMP / "normalized.mp4"
    ffmpeg.run(ffmpeg.build_normalize_cmd(
        src=str(src), out=str(out), width=1080, height=1350, seconds=5, fps=30,
    ))
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)
    stream = info["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1350), stream
    assert stream["r_frame_rate"] == "30/1", stream["r_frame_rate"]
    assert abs(float(info["format"]["duration"]) - 5) < 0.4


# --- LTX request handling, against a fake transport ------------------------

def test_ltx_builds_a_correct_request_and_post_processes_output():
    """Exercises OUR code path. Proves nothing about the real service."""
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    service = FakeLTXService(clip=make_clip(seconds=8, width=1080, height=1920, fps=25))
    gen = ltx.LTXGenerator(
        endpoint="https://api.ltx.io", api_key="test-key", opener=service
    )
    spec = SceneSpec(
        scene_id="s1", index=0, title="Opening",
        prompt="A luxury modern house at sunset with warm interior lighting",
        caption=None, seconds=7, width=1080, height=1350, quality="standard",
    )
    out = TMP / "ltx_scene.mp4"
    gen.generate(spec, out)

    # One chunk, generated at 8s because 7 is not an allowed duration.
    assert len(service.submissions) == 1, service.submissions
    sent = service.submissions[0]
    assert sent["prompt"] == spec.prompt, "the prompt must reach the provider"
    assert sent["duration"] == 8, sent["duration"]
    assert sent["model"] == config.LTX_MODEL_STANDARD
    # 4:5 is unsupported upstream, so portrait is requested and cropped after.
    assert sent["resolution"] == "1080x1920", sent["resolution"]
    assert sent["_endpoint"] == "text-to-video"
    assert sent["_auth"] == "Bearer test-key"

    # And the delivered clip matches what ReelForge asked for, not the model.
    assert out.exists()
    duration = ffmpeg.probe_duration(str(out))
    assert duration is not None and abs(duration - 7) < 0.4, duration
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
         "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
    assert (info["streams"][0]["width"], info["streams"][0]["height"]) == (1080, 1350)


def test_ltx_uses_image_to_video_when_a_reference_exists():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    still = TMP / "ref.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=s=640x480:d=1", "-frames:v", "1", str(still)],
                   check=True)
    service = FakeLTXService(clip=make_clip(seconds=6, width=1080, height=1920, fps=25))
    gen = ltx.LTXGenerator(
        endpoint="https://api.ltx.io", api_key="k", opener=service
    )
    spec = SceneSpec(
        scene_id="s1", index=0, title="t", prompt="p", caption=None,
        seconds=6, width=1080, height=1920, image_path=still,
    )
    gen.generate(spec, TMP / "ltx_img.mp4")
    sent = service.submissions[0]
    assert sent["_endpoint"] == "image-to-video"
    assert sent["image_uri"].startswith("data:image/png;base64,")


def test_ltx_chunks_a_long_scene_into_multiple_calls():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    service = FakeLTXService(clip=make_clip(seconds=10, width=1080, height=1920, fps=25))
    gen = ltx.LTXGenerator(endpoint="https://api.ltx.io", api_key="k", opener=service)
    spec = SceneSpec(
        scene_id="s1", index=0, title="t", prompt="p", caption=None,
        seconds=15, width=1080, height=1920, quality="high",
    )
    out = TMP / "ltx_long.mp4"
    gen.generate(spec, out)

    # high -> pro, which caps at 10s, so a 15s scene needs two calls.
    assert len(service.submissions) == 2, service.submissions
    assert all(s["model"] == config.LTX_MODEL_HIGH for s in service.submissions)
    assert sum(s["duration"] for s in service.submissions) >= 15
    # The customer still gets exactly 15 seconds.
    duration = ffmpeg.probe_duration(str(out))
    assert duration is not None and abs(duration - 15) < 0.5, duration


# --- 6. generation failure handling ----------------------------------------

def test_ltx_failure_does_not_leak_provider_detail():
    service = FakeLTXService(clip=TMP / "unused.mp4", fail_status="failed")
    gen = ltx.LTXGenerator(endpoint="https://api.ltx.io", api_key="secret-key",
                           opener=service)
    spec = SceneSpec(
        scene_id="s1", index=0, title="t", prompt="p", caption=None,
        seconds=6, width=1080, height=1920,
    )
    try:
        gen.generate(spec, TMP / "fail.mp4")
    except RenderError as exc:
        message = str(exc)
        assert "internal detail that must not leak" not in message
        assert "secret-key" not in message
        assert "api.ltx.io" not in message
        assert "could not generate" in message
    else:
        raise AssertionError("a failed job was treated as success")


def test_redaction_strips_the_api_key():
    original = config.LTX_API_KEY
    config.LTX_API_KEY = "super-secret"
    try:
        assert "super-secret" not in ltx.redact("key=super-secret failed")
        assert "***" in ltx.redact("key=super-secret failed")
    finally:
        config.LTX_API_KEY = original


def test_http_errors_map_to_safe_messages():
    import urllib.error

    gen = ltx.LTXGenerator(endpoint="https://api.ltx.io", api_key="k")
    cases = {401: "credentials", 429: "rate limiting", 500: "temporarily unavailable",
             422: "settings"}
    for code, expected in cases.items():
        exc = urllib.error.HTTPError(
            "https://api.ltx.io/v2/text-to-video", code, "err", {},
            __import__("io").BytesIO(json.dumps(
                {"type": "error", "error": {"message": "raw upstream detail"}}
            ).encode()),
        )
        mapped = gen._api_error(exc)
        assert expected in str(mapped), (code, str(mapped))
        assert "raw upstream detail" not in str(mapped)


# --- 7 & 8. retry behaviour and scene isolation ----------------------------

def test_one_failed_scene_does_not_discard_the_others():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    scenes = scenes_for([3, 3, 3, 3, 3])
    generator = CountingGenerator(fail_indices={1})
    hooks = RecordingHooks()
    try:
        render.render_reel(
            scenes=scenes, images={}, aspect_ratio="9:16",
            work_dir=TMP / "iso_work", out_path=TMP / "iso.mp4",
            generator=generator, hooks=hooks,
        )
    except render.SceneGenerationError as exc:
        # The customer-facing message names the scene, not a stack trace.
        assert "Scene 2" in str(exc), str(exc)
        assert len(exc.failures) == 1
        assert exc.failures[0].index == 1
    else:
        raise AssertionError("a failed scene did not fail the render")

    # Every other scene generated and was cached.
    assert hooks.succeeded == ["scene_0", "scene_2", "scene_3", "scene_4"]
    assert [s for s, _ in hooks.failed] == ["scene_1"]
    # And the audit log recorded both outcomes.
    assert ("scene_1", "failed") in hooks.logs
    assert ("scene_0", "succeeded") in hooks.logs


def test_retry_regenerates_only_the_failed_scene():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    scenes = scenes_for([3, 3, 3, 3, 3])

    # First pass: scene 2 fails, the rest are cached by the hooks.
    first = CountingGenerator(fail_indices={1})
    hooks = RecordingHooks()
    try:
        render.render_reel(
            scenes=scenes, images={}, aspect_ratio="9:16",
            work_dir=TMP / "retry_work1", out_path=TMP / "retry1.mp4",
            generator=first, hooks=hooks,
        )
    except render.SceneGenerationError:
        pass
    assert len(first.calls) == 5

    # Retry: the four cached clips are reused and only scene 2 is generated.
    second = CountingGenerator()
    out = TMP / "retry2.mp4"
    render.render_reel(
        scenes=scenes, images={}, aspect_ratio="9:16",
        work_dir=TMP / "retry_work2", out_path=out,
        generator=second, hooks=hooks,
    )
    assert second.calls == ["scene_1"], second.calls
    assert sorted(hooks.reused) == ["scene_0", "scene_2", "scene_3", "scene_4"]
    # And the reel is complete and the right length.
    assert out.exists()
    duration = ffmpeg.probe_duration(str(out))
    assert duration is not None and abs(duration - 15) < 0.8, duration


def test_fingerprint_changes_only_when_the_pixels_would():
    base = SceneSpec(
        scene_id="s1", index=0, title="t", prompt="p", caption=None,
        seconds=6, width=1080, height=1920, quality="standard",
    )
    same = render.scene_fingerprint(base, "ltx")
    # Identical inputs must hit the cache.
    assert render.scene_fingerprint(base, "ltx") == same
    # The index is presentational, not generative, so it must not bust it.
    import dataclasses
    assert render.scene_fingerprint(dataclasses.replace(base, index=4), "ltx") == same
    # These all change the output.
    for field, value in [("prompt", "different"), ("seconds", 7),
                         ("width", 1920), ("quality", "high"),
                         ("caption", "hello")]:
        changed = dataclasses.replace(base, **{field: value})
        assert render.scene_fingerprint(changed, "ltx") != same, field
    # So does switching provider.
    assert render.scene_fingerprint(base, "mock") != same


def test_cancellation_stops_between_scenes():
    if not HAVE_FFMPEG:
        print("  (skipped: ffmpeg not on PATH)")
        return
    scenes = scenes_for([2, 2, 2, 2])
    generator = CountingGenerator()
    hooks = RecordingHooks(cancel_after=2)
    try:
        render.render_reel(
            scenes=scenes, images={}, aspect_ratio="9:16",
            work_dir=TMP / "cancel_work", out_path=TMP / "cancel.mp4",
            generator=generator, hooks=hooks,
        )
    except render.RenderCancelled:
        pass
    else:
        raise AssertionError("cancellation was ignored")
    # It stopped early rather than finishing every scene.
    assert len(generator.calls) < len(scenes), generator.calls


def demo():
    # Keep the polling loop from actually sleeping.
    ltx.time.sleep = lambda *_: None
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} generation checks passed")
    if not HAVE_FFMPEG:
        print("NOTE: ffmpeg was not on PATH; video-producing checks were skipped.")


if __name__ == "__main__":
    try:
        demo()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
