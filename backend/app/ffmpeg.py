"""FFmpeg command construction and execution.

The lowest layer: builds argument lists and runs them. Knows nothing about
projects, storage, or providers, which is what makes it testable without a
database or a running server.

FFmpeg stays the video assembly layer regardless of which provider generates
the individual clips.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FPS = 30
# 16 frames at 30fps is 0.533s: inside the 0.4–0.6s range, and even, so the
# dissolve can be split evenly across the scene boundary without a half frame.
CROSSFADE_FRAMES = 16
CROSSFADE_SECONDS = CROSSFADE_FRAMES / FPS
# Scene-card background hues, cycled by scene index so a reel without uploads
# still has visual variety.
CARD_COLORS = ["0x1e1b4b", "0x312e81", "0x4c1d95", "0x1e293b", "0x3b0764", "0x172554"]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


class RenderError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def find_font() -> str | None:
    """Absolute path to a bold TTF, or None.

    drawtext needs an explicit fontfile on any machine without a working
    fontconfig setup, which includes a default Windows install.
    """
    override = os.environ.get("REELFORGE_FONT")
    candidates = [override, *FONT_CANDIDATES] if override else FONT_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            return path.replace("\\", "/")
    return None


def _font_arg() -> str:
    """`fontfile=` fragment for drawtext.

    The value must be single-quoted: a Windows drive colon is otherwise read
    as the separator before the next filter option, whether or not it is
    backslash-escaped. Verified against ffmpeg 8.
    """
    font = find_font()
    if font is None:
        raise RenderError(
            "no usable font found for captions. Install DejaVu/Arial or set the"
            " REELFORGE_FONT environment variable to a .ttf path"
        )
    return "fontfile='" + font.replace(":", "\\:") + "':"


def escape_drawtext(text: str) -> str:
    """Escape caption text for a single-quoted drawtext `text=` value.

    Apostrophes cannot be escaped inside a single-quoted filtergraph value,
    so they become typographic quotes — which is what a caption should use
    anyway.
    """
    out = (text or "").replace("\r", " ").replace("\n", " ").strip()
    out = out.replace("'", "\u2019")
    for ch in ("\\", ":", "%", "[", "]", ",", ";"):
        out = out.replace(ch, "\\" + ch)
    return out


def _drawtext(caption: str, width: int, height: int, big: bool) -> str:
    size = max(28, width // (12 if big else 22))
    return (
        "drawtext=" + _font_arg()
        + f"text='{escape_drawtext(caption)}'"
        # expansion=none keeps %, {} and friends literal instead of being
        # read as drawtext's own text-expansion syntax.
        + ":expansion=none"
        + f":fontcolor=white:fontsize={size}"
        + f":x=(w-text_w)/2:y=h-(h/{'2.1' if big else '6'})"
        + f":box=1:boxcolor=black@{'0.0' if big else '0.45'}:boxborderw=24"
        + ":line_spacing=12"
    )


def _even(value: int) -> int:
    """H.264 needs even dimensions. Round down so a crop never exceeds the frame."""
    value = int(value)
    return value if value % 2 == 0 else value - 1


def _motion(index: int, progress: str) -> tuple[str, str, str]:
    """Zoom and pan for one scene. The path repeats every five scenes.

    A larger crop window shows more of the photo (zoomed out). The widest
    window stays under the cover scale so the crop never asks for pixels
    the scaled photo does not have.
    """
    kind = ("zoom_out", "pan_x", "zoom_in", "zoom_out_pan", "drift_y")[index % 5]
    if kind == "zoom_out":
        # Gentle zoom-out, with a small upward drift.
        return (
            f"(1.02+0.06*{progress})",
            f"(in_w-out_w)*(0.28+0.44*{progress})",
            f"(in_h-out_h)*(0.58-0.16*{progress})",
        )
    if kind == "pan_x":
        # Hold the scale and travel across the spare width.
        return (
            "1.04",
            f"(in_w-out_w)*(0.18+0.64*{progress})",
            "(in_h-out_h)*0.50",
        )
    if kind == "zoom_in":
        return (
            f"(1.08-0.06*{progress})",
            f"(in_w-out_w)*(0.42+0.16*{progress})",
            "(in_h-out_h)*0.48",
        )
    if kind == "zoom_out_pan":
        return (
            f"(1.03+0.05*{progress})",
            f"(in_w-out_w)*(0.72-0.44*{progress})",
            "(in_h-out_h)*0.42",
        )
    return (
        f"(1.06-0.03*{progress})",
        "(in_w-out_w)*0.50",
        f"(in_h-out_h)*(0.22+0.56*{progress})",
    )


def build_still_clip_cmd(
    image: str, out: str, seconds: int, width: int, height: int, caption: str | None,
    index: int = 0,
) -> list[str]:
    """Slow Ken Burns move over an uploaded still.

    The photo is scaled to cover the frame, preserving its aspect ratio, then
    a window eases across the spare margin. The move follows the frame index
    and the scene index, so each scene takes a different path and a re-render
    repeats that path.

    zoompan does not do this on a still: it holds the first zoom step for the
    whole clip. An animated crop is the move that actually reaches the picture.
    """
    frames = max(1, seconds * FPS)
    span = max(frames - 1, 1)
    # Just enough spare image for a 6–8% move plus a short drift.
    cover_w = _even(round(width * 1.16))
    cover_h = _even(round(height * 1.16))
    progress = f"n/{span}"
    zoom, x, y = _motion(index, progress)
    # Commas inside min() have to be escaped or ffmpeg splits the filtergraph.
    window_w = f"min(in_w\\,trunc({width}*{zoom}/2)*2)"
    window_h = f"min(in_h\\,trunc({height}*{zoom}/2)*2)"
    chain = [
        f"scale={cover_w}:{cover_h}:force_original_aspect_ratio=increase",
        f"crop=w='{window_w}':h='{window_h}':x='{x}':y='{y}'",
        f"scale={width}:{height}:flags=lanczos",
    ]
    if caption:
        chain.append(_drawtext(caption, width, height, big=False))
    chain.append("format=yuv420p")
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", image,
        "-t", str(seconds),
        "-vf", ",".join(chain),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-an",
        out,
    ]


def build_card_clip_cmd(
    out: str, seconds: int, width: int, height: int, caption: str, index: int
) -> list[str]:
    """Typographic colour card — used when a scene has no still."""
    color = CARD_COLORS[index % len(CARD_COLORS)]
    chain = [_drawtext(caption, width, height, big=True), "format=yuv420p"]
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"color=c={color}:s={width}x{height}:d={seconds}:r={FPS}",
        "-t", str(seconds),
        "-vf", ",".join(chain),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-an",
        out,
    ]


def build_concat_list(paths: list[str]) -> str:
    """Body of a concat-demuxer list file. Single quotes must be escaped."""
    quote, backslash = chr(39), chr(92)
    escaped = quote + backslash + quote + quote
    return "".join(f"file '{p.replace(quote, escaped)}'\n" for p in paths)


def build_crossfade_cmd(
    paths: list[str], durations: list[int], out: str,
) -> list[str]:
    """Dissolve between clips and keep the reel at the sum of `durations`.

    Each join overlaps by CROSSFADE_FRAMES, centered on the scene boundary.
    The overlap is made of cloned edge frames, so the dissolve never falls
    through to black, and the picture still ends at exactly sum(durations).
    """
    if len(paths) != len(durations) or len(paths) < 2:
        raise ValueError("a crossfade needs at least two clips")
    half = CROSSFADE_FRAMES // 2
    filters: list[str] = []
    for i, seconds in enumerate(durations):
        start = 0 if i == 0 else half
        stop = 0 if i == len(durations) - 1 else half
        pad: list[str] = []
        if start:
            pad += [f"start_mode=clone", f"start_duration={start / FPS:.6f}"]
        if stop:
            pad += [f"stop_mode=clone", f"stop_duration={stop / FPS:.6f}"]
        chain = ",".join([
            "tpad=" + ":".join(pad),
            "setpts=PTS-STARTPTS",
            f"fps={FPS}",
            "format=yuv420p",
        ])
        filters.append(f"[{i}:v]{chain}[v{i}]")

    running = durations[0] * FPS + half
    current = "v0"
    for i in range(1, len(durations)):
        start = half
        stop = 0 if i == len(durations) - 1 else half
        length = durations[i] * FPS + start + stop
        offset = running - CROSSFADE_FRAMES
        if offset < 0:
            raise ValueError("scene is shorter than the crossfade")
        label = f"x{i}"
        filters.append(
            f"[{current}][v{i}]xfade=transition=fade"
            f":duration={CROSSFADE_SECONDS:.6f}:offset={offset / FPS:.6f}[{label}]"
        )
        running = running + length - CROSSFADE_FRAMES
        current = label

    total_frames = sum(durations) * FPS
    # xfade lands one frame short of the arithmetic length. Clone that frame
    # and trim so the container duration is exactly sum(durations).
    filters.append(
        f"[{current}]tpad=stop_mode=clone:stop_duration=0.1,"
        f"trim=end_frame={total_frames},setpts=PTS-STARTPTS[vout]"
    )
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for path in paths:
        cmd += ["-i", path]
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        "-frames:v", str(total_frames),
        "-r", str(FPS),
        "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-an",
        out,
    ]
    return cmd


def build_concat_cmd(list_file: str, out: str) -> list[str]:
    """Join clips without re-encoding. Requires identical codec params."""
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", out,
    ]


def build_music_mux_cmd(
    video: str, music: str, out: str, total_seconds: int,
    volume: float = 0.8, fade_out: int = 2,
) -> list[str]:
    """Lay a soundtrack over finished video without re-encoding the video.

    `apad` extends a short track with silence. `-shortest` then ends the file
    when the video ends, so a long track is trimmed and a short one cannot
    shrink the reel. Volume and the end fade stay on the video's timeline.
    """
    filters = [f"volume={max(0.0, min(volume, 2.0)):.3f}"]
    if fade_out > 0 and total_seconds > fade_out:
        filters.append(f"afade=t=out:st={total_seconds - fade_out}:d={fade_out}")
    filters.append("apad")
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video, "-i", music,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-af", ",".join(filters),
        "-shortest", "-movflags", "+faststart", out,
    ]


def build_normalize_cmd(
    src: str, out: str, width: int, height: int, seconds: float | None = None,
    fps: int = FPS, caption: str | None = None,
) -> list[str]:
    """Conform any clip to exact dimensions, frame rate and length.

    Generation providers do not offer every aspect ratio or frame rate that
    ReelForge does — LTX, for instance, produces only 16:9 and 9:16 at 24, 25,
    48 or 50fps. This is where that gap is closed instead of letting it reach
    the customer: scale to cover, centre-crop to the exact frame, and resample
    to the pipeline frame rate.

    `seconds` trims the output. Concatenation later stream-copies, so every
    clip must agree on all three properties or the join is corrupt.
    """
    chain = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"fps={fps}",
    ]
    if caption:
        chain.append(_drawtext(caption, width, height, big=False))
    chain.append("format=yuv420p")

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += [
        "-vf", ",".join(chain),
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-an",
        out,
    ]
    return cmd


def probe_duration(path: str) -> float | None:
    """Seconds of media at `path`, or None if ffprobe cannot read it."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def build_finalize_cmd(video: str, out: str) -> list[str]:
    """Remux for streaming when there is no soundtrack to add."""
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video, "-c", "copy", "-movflags", "+faststart", out,
    ]


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RenderError(f"{cmd[0]} failed: " + " | ".join(tail))
