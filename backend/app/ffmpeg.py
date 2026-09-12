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


def build_still_clip_cmd(
    image: str, out: str, seconds: int, width: int, height: int, caption: str | None
) -> list[str]:
    """Ken Burns push-in over an uploaded still."""
    frames = max(1, seconds * FPS)
    chain = [
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"zoompan=z='min(zoom+0.0012,1.18)':d={frames}:x='iw/2-(iw/zoom/2)'"
        f":y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={FPS}",
    ]
    if caption:
        chain.append(_drawtext(caption, width, height, big=False))
    chain.append("format=yuv420p")
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", image,
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
    """Lay a soundtrack over finished video without re-encoding the video."""
    filters = [f"volume={max(0.0, min(volume, 2.0)):.3f}"]
    if fade_out > 0 and total_seconds > fade_out:
        filters.append(f"afade=t=out:st={total_seconds - fade_out}:d={fade_out}")
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video, "-i", music,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-af", ",".join(filters),
        "-shortest", "-movflags", "+faststart", out,
    ]


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
