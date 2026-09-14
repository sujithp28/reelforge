#!/usr/bin/env python3
"""
ReelForge — LTX-Video 2B distilled, 49-frame T4 benchmark.

Measures: total wall time, peak VRAM, output resolution/FPS/duration/size.

Run from /kaggle/working:
    python /kaggle/working/reelforge/kaggle/ltx_bench_49f.py
"""

import sys
import time
import threading
import subprocess
from pathlib import Path

KAGGLE_WORKING = Path("/kaggle/working")
LTX_REPO = KAGGLE_WORKING / "LTX-Video"
CONFIG_PATH = Path(__file__).parent / "configs" / "bench-t4-49f-noupscaler.yaml"
# LTX treats --output_path as a directory and picks its own filename inside it.
OUTPUT_DIR = KAGGLE_WORKING / "ltx_bench_49f_output"
VRAM_POLL_SECONDS = 0.5

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


def gpu0_used_mib():
    """GPU 0 memory in use across all processes, via nvidia-smi. None if unavailable."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
                "-i",
                "0",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return int(result.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def poll_gpu0_memory(stop, samples):
    while not stop.is_set():
        used = gpu0_used_mib()
        if used is not None:
            samples.append(used)
        stop.wait(VRAM_POLL_SECONDS)


def find_output_video(since):
    """Newest .mp4 in OUTPUT_DIR written during this run, so older runs are never reported."""
    if not OUTPUT_DIR.is_dir():
        return None
    videos = [p for p in OUTPUT_DIR.glob("*.mp4") if p.stat().st_mtime >= since]
    return max(videos, key=lambda p: p.stat().st_mtime, default=None)


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
        str(LTX_REPO / "inference.py"),
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
        str(OUTPUT_DIR),
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

    baseline_mib = gpu0_used_mib()
    vram_samples = []
    stop_polling = threading.Event()
    poller = threading.Thread(
        target=poll_gpu0_memory, args=(stop_polling, vram_samples), daemon=True
    )

    run_started_at = time.time()
    start = time.perf_counter()

    print(f"[{time.strftime('%H:%M:%S')}] Starting inference...")

    poller.start()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(KAGGLE_WORKING),
            text=True,
        )
    finally:
        stop_polling.set()
        poller.join()

    total_time = time.perf_counter() - start

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    if vram_samples:
        peak_mib = max(vram_samples)
        baseline_text = (
            f"{baseline_mib / 1024:.2f} GB" if baseline_mib is not None else "N/A"
        )
        print(
            f"Peak VRAM:     {peak_mib / 1024:.2f} GB "
            f"(GPU 0, nvidia-smi, {len(vram_samples)} samples; "
            f"baseline before inference: {baseline_text})"
        )
    else:
        print("Peak VRAM:     N/A (nvidia-smi unavailable)")

    if result.returncode != 0:
        print(f"✗ inference.py failed (exit {result.returncode})")
        return

    video_path = find_output_video(run_started_at)

    if video_path is not None:
        duration, width, height, fps = probe_video(video_path)
        size_mb = video_path.stat().st_size / 1024**2
    else:
        duration, width, height, fps = None, None, None, None
        size_mb = None

    print(f"Total time:    {total_time:.1f}s ({total_time / 60:.2f} min)")

    if video_path is not None:
        print(f"Output file:   {video_path}")
    else:
        print(f"Output file:   not found — no new .mp4 in {OUTPUT_DIR}")

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
