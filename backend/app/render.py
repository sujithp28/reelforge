"""FFmpeg render pipeline.

One scene becomes one clip, clips are concatenated, optional music is muxed
over the result. FFmpeg is expected on PATH (as the README already stated).

GENERATION ADAPTER SEAM
-----------------------
`build_scene_clip_cmd` is the only place that decides how a scene's pixels
come into existence. Today it has two strategies: a Ken Burns move over an
uploaded still, and a typographic colour card when there is no still. To
plug in a real text-to-video model (LTX, Runway, Veo), replace
`generate_scene_clip` with a call that writes an mp4 to `out` and keep the
rest of this module untouched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FPS = 30
# Scene-card background hues, cycled by scene index so a music-video style
# reel without uploads still has visual variety.
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
    """Typographic colour card — the placeholder until a video model is wired in."""
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
    return "".join(f"file '{p.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for p in paths)


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RenderError(f"{cmd[0]} failed: " + " | ".join(tail))


def generate_scene_clip(
    scene: dict, image: str | None, out: Path, width: int, height: int, index: int
) -> None:
    """Produce one scene clip. Swap this body for an LTX/Runway call."""
    seconds = max(1, int(scene["duration"]))
    if image and Path(image).exists():
        cmd = build_still_clip_cmd(
            image=image, out=str(out), seconds=seconds,
            width=width, height=height, caption=scene.get("caption"),
        )
    else:
        caption = scene.get("caption") or scene["title"]
        cmd = build_card_clip_cmd(
            out=str(out), seconds=seconds, width=width, height=height,
            caption=caption, index=index,
        )
    _run(cmd)


def render_reel(
    scenes: list[dict],
    images: dict[str, str | None],
    aspect_ratio: str,
    work_dir: Path,
    out_path: Path,
    music: str | None = None,
    on_progress=None,
) -> Path:
    """Render `scenes` into a single mp4 at `out_path`. Returns that path."""
    from .storyboard import resolution_for

    if not ffmpeg_available():
        raise RenderError("ffmpeg was not found on PATH")
    if not scenes:
        raise RenderError("nothing to render: this project has no scenes")

    width, height = resolution_for(aspect_ratio)
    work_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []

    for i, scene in enumerate(scenes):
        clip = work_dir / f"scene_{i:02d}.mp4"
        generate_scene_clip(scene, images.get(scene["id"]), clip, width, height, i)
        clips.append(clip)
        if on_progress:
            # Leave the last 15% for concat and mux.
            on_progress(int(5 + 80 * (i + 1) / len(scenes)))

    list_file = work_dir / "concat.txt"
    list_file.write_text(build_concat_list([str(c) for c in clips]), encoding="utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    silent = work_dir / "silent.mp4"
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(silent),
    ])
    if on_progress:
        on_progress(90)

    if music and Path(music).exists():
        _run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(silent), "-i", music,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-af", "afade=t=out:st=%d:d=1.5" % max(0, sum(s["duration"] for s in scenes) - 2),
            "-shortest", "-movflags", "+faststart", str(out_path),
        ])
    else:
        _run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(silent), "-c", "copy", "-movflags", "+faststart", str(out_path),
        ])

    for leftover in list(clips) + [list_file, silent]:
        leftover.unlink(missing_ok=True)
    if on_progress:
        on_progress(100)
    return out_path
