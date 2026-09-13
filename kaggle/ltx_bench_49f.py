#!/usr/bin/env python3
"""
ReelForge — LTX-Video 2B distilled, 49-frame T4 benchmark.

Measures: total wall time, peak VRAM, output resolution/FPS/duration/size.

Run from /kaggle/working:
    python /kaggle/working/reelforge/kaggle/ltx_bench_49f.py
"""

import sys
import time
import subprocess
from pathlib import Path

KAGGLE_WORKING = Path("/kaggle/working")
LTX_REPO = KAGGLE_WORKING / "LTX-Video"
CONFIG_PATH = Path(__file__).parent / "configs" / "bench-t4-49f-noupscaler.yaml"
OUTPUT_PATH = KAGGLE_WORKING / "bench_49f_output.mp4"

PROMPT = (
    "A camera slowly pans across a sunlit forest path, "
    "dappled golden light filtering through leaves, cinematic, 4K."
)

HEIGHT, WIDTH, FRAMES, FPS, SEED = 832, 480, 49, 24, 42


def check_prerequisites():
    issues = []

    if not LTX_REPO.exists():
        issues.append(f"LTX-Video repo not found at {LTX_REPO}")

    checkpoint = Path("/root/.cache/reelforge-ltx/ltxv-2b-0.9.8-distilled.safetensors")
    if not checkpoint.exists():
        issues.append("Checkpoint not found — run the LTX setup/download step")

    if not Path("/root/.cache/reelforge-ltx/text_encoder").exists():
        issues.append("Text encoder not found — run the text encoder setup step")

    if not CONFIG_PATH.exists():
        issues.append(f"Config not found: {CONFIG_PATH}")

    if issues:
        print("PREREQUISITE ERRORS:")
        for issue in issues:
            print(f"  ✗ {issue}")
        sys.exit(1)

    print("Prerequisites OK")


def gpu_info():
    try:
        import torch

        if not torch.cuda.is_available():
            print("ERROR: CUDA GPU is not available.")
            sys.exit(1)

        torch.cuda.reset_peak_memory_stats()

        free, total = torch.cuda.mem_get_info()

        print(f"GPU:  {torch.cuda.get_device_name(0)}")
        print(
            f"VRAM: {free / 1024**3:.1f} GB free / "
            f"{total / 1024**3:.1f} GB total"
        )

    except Exception as exc:
        print(f"GPU information error: {exc}")
        sys.exit(1)


def peak_vram_gb():
    try:
        import torch

        return torch.cuda.max_memory_allocated() / 1024**3

    except Exception:
        return None


def probe_video(path):
    try:
        import json

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        for stream in json.loads(result.stdout).get("streams", []):
            if stream.get("codec_type") == "video":
                numerator, denominator = stream.get(
                    "r_frame_rate", "24/1"
                ).split("/")

                return (
                    float(stream.get("duration", 0)),
                    int(stream.get("width", 0)),
                    int(stream.get("height", 0)),
                    round(int(numerator) / int(denominator), 2),
                )

    except Exception as exc:
        print(f"ffprobe: {exc}")

    return None, None, None, None


def run_benchmark():
    check_prerequisites()
    gpu_info()

    cmd = [
        sys.executable,
        str(LTX_REPO / "ltx_video" / "inference.py"),
        "--pipeline_config",
        str(CONFIG_PATH),
        "--prompt",
        PROMPT,
        "--height",
        str(HEIGHT),
        "--width",
        str(WIDTH),
        "--num_frames",
        str(FRAMES),
        "--frame_rate",
        str(FPS),
        "--seed",
        str(SEED),
        "--output_path",
        str(OUTPUT_PATH),
        "--offload_to_cpu",
        "True",
    ]

    print("\n" + "=" * 60)
    print("ReelForge LTX-Video 2B Distilled — 49-frame T4 Benchmark")
    print(
        f"Resolution: {WIDTH}x{HEIGHT}  "
        f"Frames: {FRAMES} (~{FRAMES / FPS:.1f}s)  "
        f"Steps: 8"
    )
    print("=" * 60)

    start = time.perf_counter()

    print(f"[{time.strftime('%H:%M:%S')}] Starting inference...")

    result = subprocess.run(
        cmd,
        cwd=str(KAGGLE_WORKING),
        text=True,
    )

    total_time = time.perf_counter() - start

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    if result.returncode != 0:
        print(f"✗ inference.py failed (exit {result.returncode})")
        return

    vram = peak_vram_gb()

    if OUTPUT_PATH.exists():
        duration, width, height, fps = probe_video(OUTPUT_PATH)
        size_mb = OUTPUT_PATH.stat().st_size / 1024**2
    else:
        duration, width, height, fps = None, None, None, None
        size_mb = None

    print(f"Total time:    {total_time:.1f}s ({total_time / 60:.2f} min)")

    if vram is not None:
        print(f"Peak VRAM:     {vram:.2f} GB")

    if size_mb is not None:
        print(f"File size:     {size_mb:.1f} MB")

    if width:
        print(f"Resolution:    {width}x{height}")

    if fps:
        print(f"FPS:           {fps}")

    if duration:
        print(f"Duration:      {duration:.2f}s")

    print()

    if total_time < 300:
        print(
            f"✓ {total_time / 60:.1f} min < 5 min "
            "→ PROCEED to production-representative test"
        )
    elif total_time < 600:
        print(
            f"⚠ {total_time / 60:.1f} min = 5–10 min "
            "→ INVESTIGATE before proceeding"
        )
    else:
        print(
            f"✗ {total_time / 60:.1f} min > 10 min "
            "→ STOP and reassess architecture"
        )

    print("=" * 60)
    print("Benchmark complete. Do not automatically start another test.")


if __name__ == "__main__":
    run_benchmark()
