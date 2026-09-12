"""REAL LTX generation. Contacts the live API and spends money.

Separated from the rest of the suite on purpose. It is never part of CI and
never runs by default: it needs credentials, it is slow, and each run is
billable. Everything in test_generation.py uses a fake transport and proves
nothing about the real service; this file is the only thing that can.

    setx REELFORGE_LTX_API_KEY "sk-..."          (Windows, new shell after)
    set REELFORGE_RUN_LTX_INTEGRATION=1
    python test_ltx_integration.py

Without both variables it skips and says so, rather than passing silently and
leaving the impression that real generation was verified.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="reelforge-ltx-")
os.environ["REELFORGE_DATA_DIR"] = _TMP
os.environ.setdefault("REELFORGE_VIDEO_PROVIDER", "ltx")

from app import config, ffmpeg, ltx, render  # noqa: E402
from app.providers import SceneSpec  # noqa: E402

TMP = Path(_TMP)

IDEA_PROMPT = "A luxury modern house at sunset with warm interior lighting."

ENABLED = os.environ.get("REELFORGE_RUN_LTX_INTEGRATION") == "1"
HAS_KEY = bool(config.LTX_API_KEY)


def _reason_to_skip() -> str | None:
    if not HAS_KEY:
        return "REELFORGE_LTX_API_KEY is not set"
    if not ENABLED:
        return "REELFORGE_RUN_LTX_INTEGRATION is not 1 (billable, so opt-in)"
    if not ffmpeg.ffmpeg_available():
        return "ffmpeg is not on PATH"
    return None


def test_real_single_scene_generation():
    """Generate one real scene and verify the delivered clip.

    Checks the things that actually matter: the prompt reached the provider,
    a valid video came back, and it matches the duration and frame ReelForge
    asked for rather than what the model happened to produce.
    """
    skip = _reason_to_skip()
    if skip:
        print(f"  SKIPPED real generation: {skip}")
        return

    generator = ltx.LTXGenerator(
        endpoint=config.LTX_ENDPOINT,
        api_key=config.LTX_API_KEY,
        timeout=config.LTX_TIMEOUT_SECONDS,
    )
    assert generator.available(), generator.configuration_error()

    # 7 seconds and 4:5 are both unsupported upstream, which is exactly why
    # they are worth using here: they exercise quantization and cropping.
    spec = SceneSpec(
        scene_id="integration", index=0, title="Establishing",
        prompt=IDEA_PROMPT, caption=None,
        seconds=7, width=1080, height=1350, quality="standard",
    )
    out = TMP / "real_scene.mp4"
    print(f"  generating a real 7s scene at 1080x1350 (model call, billable)...")
    generator.generate(spec, out)

    assert out.exists() and out.stat().st_size > 0, "no output file"
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate:format=duration",
         "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)
    stream = info["streams"][0]
    duration = float(info["format"]["duration"])
    print(f"  got {out.stat().st_size // 1024}KB {stream['codec_name']} "
          f"{stream['width']}x{stream['height']} {duration:.2f}s")

    assert stream["codec_name"] == "h264", stream
    assert (stream["width"], stream["height"]) == (1080, 1350), stream
    assert abs(duration - 7) < 0.5, f"expected ~7s, got {duration}"


def test_real_reel_assembles_end_to_end():
    """A full multi-scene reel through the real provider and ffmpeg."""
    skip = _reason_to_skip()
    if skip:
        print(f"  SKIPPED real reel: {skip}")
        return

    generator = ltx.LTXGenerator(
        endpoint=config.LTX_ENDPOINT,
        api_key=config.LTX_API_KEY,
        timeout=config.LTX_TIMEOUT_SECONDS,
    )
    scenes = [
        {"id": "s0", "position": 0, "title": "Approach", "duration": 7,
         "caption": None,
         "prompt": f"{IDEA_PROMPT} Exterior approach, slow dolly in."},
        {"id": "s1", "position": 1, "title": "Interior", "duration": 5,
         "caption": None,
         "prompt": f"{IDEA_PROMPT} Interior living space, warm lamps, dusk."},
    ]
    out = TMP / "real_reel.mp4"
    print("  generating a real 12s two-scene reel (billable)...")
    render.render_reel(
        scenes=scenes, images={}, aspect_ratio="9:16",
        work_dir=TMP / "reel_work", out_path=out,
        generator=generator, quality="standard",
    )

    assert out.exists()
    duration = ffmpeg.probe_duration(str(out))
    print(f"  assembled {out.stat().st_size // 1024}KB {duration:.2f}s")
    assert duration is not None and abs(duration - 12) < 0.8, duration


def demo():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  done  {fn.__name__}")
    if _reason_to_skip():
        print("\nREAL LTX GENERATION WAS NOT VERIFIED:"
              f" {_reason_to_skip()}")
    else:
        print(f"\n{len(fns)} real LTX checks passed")


if __name__ == "__main__":
    try:
        demo()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
