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

from . import typography

FPS = 30
# 16 frames at 30fps is 0.533s: inside the 0.4–0.6s range, and even, so the
# dissolve can be split evenly across the scene boundary without a half frame.
CROSSFADE_FRAMES = 16
CROSSFADE_SECONDS = CROSSFADE_FRAMES / FPS
# Spare image around the output frame. Just enough for the slow zoom and the
# few-percent drift. A larger scale would crop the room before the move starts.
COVER_SCALE = 1.12
# Baked into the mock clip fingerprint so a look change cannot reuse a stale clip.
STILL_LOOK_ID = "cinematic-v3.3"
# Opening title only. Half a second in, and fully gone before the crossfade
# reaches back into this clip.
TITLE_FADE_SECONDS = 0.5
# One wipe on every join. A dissolve blends both pictures and reads as a
# double exposure. Same-asset neighbors stay hard cuts and never reach this.
TRANSITION_CYCLE = ("wipeleft",)
# Consecutive names are different families, so a reel does not alternate
# zoom, pan, zoom, pan. Unknown photos follow this order.
MOTION_PATHS = (
    "push_in",
    "drift_lr",
    "diagonal",
    "pull_out",
    "drift_ud",
    "push_drift",
    "drift_rl",
    "drift_du",
    "pull_drift",
)
# Each value is a permutation of MOTION_PATHS. Wide and tall rooms lead with
# a slow zoom or a short drift, because a long pan would cross the room.
_ORIENTATION_ORDER = {
    "landscape": (
        "drift_lr", "push_in", "diagonal", "drift_rl", "pull_out",
        "drift_ud", "push_drift", "drift_du", "pull_drift",
    ),
    "wide": (
        "push_in", "drift_lr", "diagonal", "drift_rl", "pull_out",
        "drift_ud", "push_drift", "drift_du", "pull_drift",
    ),
    "portrait": (
        "drift_ud", "push_in", "drift_lr", "diagonal", "pull_out",
        "drift_du", "push_drift", "drift_rl", "pull_drift",
    ),
    "tall": (
        "drift_ud", "push_in", "diagonal", "drift_lr", "pull_out",
        "drift_du", "push_drift", "drift_rl", "pull_drift",
    ),
    "square": (
        "diagonal", "drift_lr", "pull_out", "drift_ud", "push_in",
        "drift_rl", "pull_drift", "drift_du", "push_drift",
    ),
}
# Scene-card background hues, cycled by scene index so a reel without uploads
# still has visual variety.
CARD_COLORS = ["0x1e1b4b", "0x312e81", "0x4c1d95", "0x1e293b", "0x3b0764", "0x172554"]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
# Scene titles prefer a serif already on the machine. Subtitles use a smaller
# sans so the two lines do not compete. Title cards keep the bold list above.
# Nothing is downloaded.
TITLE_FONT_CANDIDATES = [
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
SUBTITLE_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
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


def _fontfile_arg(font: str | None) -> str:
    """`fontfile=` fragment for drawtext.

    The value must be single-quoted: a Windows drive colon is otherwise read
    as the separator before the next filter option, whether or not it is
    backslash-escaped. Verified against ffmpeg 8.
    """
    if font is None:
        raise RenderError(
            "no usable font found for captions. Install DejaVu/Arial or set the"
            " REELFORGE_FONT environment variable to a .ttf path"
        )
    return "fontfile='" + font.replace(":", "\\:") + "':"


def _font_arg() -> str:
    return _fontfile_arg(find_font())


def _first_font(candidates: list[str]) -> str | None:
    override = os.environ.get("REELFORGE_FONT")
    paths = [override, *candidates] if override else candidates
    for path in paths:
        if path and Path(path).exists():
            return path.replace("\\", "/")
    return None


def find_title_font() -> str | None:
    """Serif for the scene title, or the bold caption font."""
    return _first_font(TITLE_FONT_CANDIDATES) or find_font()


def find_subtitle_font() -> str | None:
    """Sans for the subtitle, so it stays quieter than the title."""
    return _first_font(SUBTITLE_FONT_CANDIDATES) or find_title_font()


def _title_font_arg() -> str:
    return _fontfile_arg(find_title_font())


def _subtitle_font_arg() -> str:
    return _fontfile_arg(find_subtitle_font())


def _title_alpha(seconds: float) -> str:
    """Fade in, hold, then fade out before the first crossfade.

    The join starts CROSSFADE_SECONDS/2 before this clip ends, because the
    dissolve is centered on the scene boundary. The line is already gone then.
    """
    fade = TITLE_FADE_SECONDS
    out_end = float(seconds) - (CROSSFADE_SECONDS / 2.0)
    if out_end <= fade * 2:
        fade = max(0.12, out_end / 3.0)
    out_start = out_end - fade
    if out_start < fade:
        fade = max(0.12, out_end / 2.0)
        out_start = max(0.0, out_end - fade)
    return (
        f"if(lt(t\\,{fade:.3f})\\,t/{fade:.3f}\\,"
        f"if(lt(t\\,{out_start:.3f})\\,1\\,"
        f"if(lt(t\\,{out_end:.3f})\\,({out_end:.3f}-t)/{fade:.3f}\\,0)))"
    )


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


def _drawtext(
    caption: str, width: int, height: int, big: bool, seconds: float | None = None,
    role: str = "title",
) -> str:
    if big:
        size = max(28, width // 12)
        return (
            "drawtext=" + _font_arg()
            + f"text='{escape_drawtext(caption)}'"
            # expansion=none keeps %, {} and friends literal instead of being
            # read as drawtext's own text-expansion syntax.
            + ":expansion=none"
            + f":fontcolor=white:fontsize={size}"
            + ":x=(w-text_w)/2:y=h-(h/2.1)"
            + ":box=1:boxcolor=black@0.0:boxborderw=24"
            + ":line_spacing=12"
        )
    # Design text on the photograph. Smaller than a title card, no plate.
    # A hairline shadow keeps the words readable on a bright wall and a dark one.
    # The subtitle sits on a fixed line under the title so either can be absent.
    if role == "subtitle":
        size = max(18, width // 48)
        y = "h*0.785"
    else:
        size = max(24, width // 32)
        y = "h*0.74-text_h"
    alpha = _title_alpha(seconds) if seconds and seconds > 0 else (
        f"if(lt(t\\,{TITLE_FADE_SECONDS:.3f})\\,t/{TITLE_FADE_SECONDS:.3f}\\,1)"
    )
    return (
        "drawtext=" + _title_font_arg()
        + f"text='{escape_drawtext(caption)}'"
        + ":expansion=none"
        + f":fontcolor=white:fontsize={size}"
        + f":x=(w-text_w)/2:y={y}"
        + ":shadowcolor=black@0.72:shadowx=0:shadowy=2"
        + ":borderw=1:bordercolor=black@0.40"
        + ":line_spacing=6"
        + f":alpha='{alpha}'"
    )


def _overlay_filters(
    text_title: str | None, text_subtitle: str | None,
    width: int, height: int, seconds: int,
) -> list[str]:
    """Scene title and subtitle. Fade timing is unchanged; only the look moves."""
    title_font = find_title_font()
    subtitle_font = find_subtitle_font()
    title, subtitle = typography.compose(
        text_title, text_subtitle, title_font, subtitle_font, width, height,
    )
    filters: list[str] = []
    if title is not None:
        filters.append(_overlay_drawtext(title, _title_font_arg(), seconds, "title"))
    if subtitle is not None:
        filters.append(_overlay_drawtext(subtitle, _subtitle_font_arg(), seconds, "subtitle"))
    return filters


def _overlay_drawtext(block: typography.TextBlock, font_arg: str, seconds: int, role: str) -> str:
    text = "\\n".join(escape_drawtext(line) for line in block.lines)
    color = "white" if role == "title" else "white@0.86"
    alpha = _title_alpha(seconds) if seconds and seconds > 0 else (
        f"if(lt(t\\,{TITLE_FADE_SECONDS:.3f})\\,t/{TITLE_FADE_SECONDS:.3f}\\,1)"
    )
    return (
        "drawtext=" + font_arg
        + f"text='{text}'"
        + ":expansion=none"
        + f":fontcolor={color}:fontsize={block.fontsize}"
        + f":x=(w-text_w)/2:y={block.y}"
        + ":shadowcolor=black@0.85:shadowx=1:shadowy=2"
        + ":borderw=1:bordercolor=black@0.55"
        + f":line_spacing={block.line_spacing}"
        + f":alpha='{alpha}'"
    )


def h264_args() -> list[str]:
    """Encoder settings for the finished picture.

    `color_range tv` keeps a JPEG still from being tagged yuvj420p, which
    some players reject. The customer never sees these flags.
    """
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-color_range", "tv",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
    ]


def _even(value: int) -> int:
    """H.264 needs even dimensions. Round down so a crop never exceeds the frame."""
    value = int(value)
    return value if value % 2 == 0 else value - 1


def orientation_from_size(width: int, height: int) -> str:
    """How the photograph is shaped. Dimensions only — no vision model.

    Wide and tall are the architectural cases: a 16:9 room, or a full-length
    portrait. Those get a shorter move than a merely landscape or portrait frame.
    """
    if width <= 0 or height <= 0:
        return "unknown"
    ratio = width / height
    if ratio >= 1.70:
        return "wide"
    if ratio >= 1.08:
        return "landscape"
    if ratio <= 1 / 1.70:
        return "tall"
    if ratio <= 1 / 1.08:
        return "portrait"
    return "square"


def image_orientation(path: str) -> str:
    """portrait, landscape, wide, tall, square, or unknown."""
    if not path or not Path(path).is_file():
        return "unknown"
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    parts = (proc.stdout or "").strip().split(",")
    if proc.returncode != 0 or len(parts) != 2:
        return "unknown"
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        return "unknown"
    return orientation_from_size(width, height)


def motion_family(kind: str) -> str:
    """Paths that would feel like the same camera move."""
    if kind in ("push_in", "pull_out"):
        return "zoom"
    if kind in ("drift_lr", "drift_rl"):
        return "horizontal"
    if kind in ("drift_ud", "drift_du"):
        return "vertical"
    if kind == "diagonal":
        return "diagonal"
    return "travel"


def motion_kind(index: int, orientation: str = "unknown") -> str:
    """Deterministic path for this scene. The same index always repeats."""
    order = _ORIENTATION_ORDER.get(orientation, MOTION_PATHS)
    return order[index % len(order)]


def transition_for(index: int) -> str:
    """The same wipe for every join. Index 0 is the first pair of scenes."""
    return TRANSITION_CYCLE[index % len(TRANSITION_CYCLE)]


def _span(start: float, end: float, ease: str) -> str:
    delta = end - start
    sign = "+" if delta >= 0 else "-"
    return f"({start:.3f}{sign}{abs(delta):.3f}*{ease})"


def _travel(width: int, height: int, orientation: str) -> tuple[int, int]:
    """How far the window may drift, in output pixels.

    This is a few percent of the reel, not a fraction of the leftover photo.
    A wide room in a vertical frame therefore stays near the middle of the
    picture instead of sliding from one edge to the other.
    """
    hx, hy = 0.042, 0.020
    if orientation == "wide":
        hx = 0.026
    elif orientation == "tall":
        hy = 0.012
    elif orientation in ("square", "unknown"):
        hx = 0.032
    return (
        max(8, _even(round(width * hx))),
        max(8, _even(round(height * hy))),
    )


def _placed(offset: str, axis: str) -> str:
    """Center the window, then ease `offset` pixels, and stay inside the photo."""
    spare = "in_w-out_w" if axis == "x" else "in_h-out_h"
    return f"max(0\\,min({spare}\\,({spare})/2+({offset})))"


def _motion(kind: str, ease: str, width: int, height: int, orientation: str) -> tuple[str, str, str]:
    """Zoom and a short drift around the center of the photograph.

    A larger zoom number is a larger window, so it shows more of the photo.
    Every zoom stays under COVER_SCALE. Horizontal-only and vertical-only
    paths do not move the other axis, so architectural lines are not asked
    to slide on a diagonal.
    """
    hx, hy = _travel(width, height, orientation)
    # Wide and tall photos are already cropped hard by the frame. Zoom less.
    far, near = (1.030, 1.010) if orientation in ("wide", "tall") else (1.042, 1.012)
    held = f"{(far + near) / 2:.3f}"
    if kind == "push_in":
        zoom = _span(far, near, ease)
        ox, oy = _span(-hx * 0.22, hx * 0.12, ease), _span(hy * 0.16, -hy * 0.08, ease)
    elif kind == "pull_out":
        zoom = _span(near, far, ease)
        ox, oy = _span(hx * 0.12, -hx * 0.22, ease), _span(-hy * 0.08, hy * 0.16, ease)
    elif kind == "drift_lr":
        zoom, ox, oy = held, _span(-hx, hx, ease), "0"
    elif kind == "drift_rl":
        zoom, ox, oy = held, _span(hx, -hx, ease), "0"
    elif kind == "drift_ud":
        zoom, ox, oy = held, "0", _span(-hy, hy, ease)
    elif kind == "drift_du":
        zoom, ox, oy = held, "0", _span(hy, -hy, ease)
    elif kind == "diagonal":
        zoom = _span(near, (near + far) / 2, ease)
        ox, oy = _span(-hx * 0.55, hx * 0.55, ease), _span(hy * 0.55, -hy * 0.55, ease)
    elif kind == "push_drift":
        zoom = _span(far, near, ease)
        ox, oy = _span(-hx * 0.7, hx * 0.45, ease), "0"
    else:
        zoom = _span(near, far, ease)
        ox, oy = _span(hx * 0.7, -hx * 0.45, ease), "0"
    return zoom, _placed(ox, "x"), _placed(oy, "y")


def build_still_clip_cmd(
    image: str, out: str, seconds: int, width: int, height: int, caption: str | None = None,
    index: int = 0, text_title: str | None = None, text_subtitle: str | None = None,
    fps: int = FPS,
) -> list[str]:
    """Slow Ken Burns move over an uploaded still.

    The photo is scaled to cover the frame, preserving its aspect ratio, then
    a window eases a short distance around its center. The move follows the
    photograph's shape and the scene index, so each scene takes a different
    path and a re-render repeats that path.

    zoompan does not do this on a still: it holds the first zoom step for the
    whole clip. An animated crop is the move that actually reaches the picture.
    """
    frames = max(1, seconds * fps)
    span = max(frames - 1, 1)
    cover_w = _even(round(width * COVER_SCALE))
    cover_h = _even(round(height * COVER_SCALE))
    # Cosine ease-in-out: the camera settles at each end instead of moving
    # at a constant speed.
    ease = f"(1-cos(PI*n/{span}))/2"
    orientation = image_orientation(image)
    zoom, x, y = _motion(motion_kind(index, orientation), ease, width, height, orientation)
    # Commas inside min() have to be escaped or ffmpeg splits the filtergraph.
    window_w = f"min(in_w\\,trunc({width}*({zoom})/2)*2)"
    window_h = f"min(in_h\\,trunc({height}*({zoom})/2)*2)"
    chain = [
        f"scale={cover_w}:{cover_h}:force_original_aspect_ratio=increase",
        f"crop=w='{window_w}':h='{window_h}':x='{x}':y='{y}'",
        f"scale={width}:{height}:flags=lanczos",
        # A light grade only. No color shift, so white walls stay white.
        # Saturation stays at the photograph. The vignette is the faint one
        # already used: a smaller angle is lighter, and it is locked to the
        # frame rather than the pan.
        "eq=contrast=1.02:saturation=1.00:gamma=1.01",
        "vignette=angle=PI/10",
    ]
    # `caption` is the older project-idea field. A photograph does not burn it.
    # Only the scene's own enabled title and subtitle are drawn.
    chain.extend(_overlay_filters(text_title, text_subtitle, width, height, seconds))
    chain.append("format=yuv420p")
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", image,
        "-t", str(seconds),
        "-vf", ",".join(chain),
        "-r", str(fps),
        *h264_args(), "-an",
        out,
    ]


def build_card_clip_cmd(
    out: str, seconds: int, width: int, height: int, caption: str, index: int,
    fps: int = FPS,
) -> list[str]:
    """Typographic colour card — used when a scene has no still."""
    color = CARD_COLORS[index % len(CARD_COLORS)]
    chain = [_drawtext(caption, width, height, big=True), "format=yuv420p"]
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"color=c={color}:s={width}x{height}:d={seconds}:r={fps}",
        "-t", str(seconds),
        "-vf", ",".join(chain),
        *h264_args(), "-an",
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
    """Wipe between clips and keep the reel at the sum of `durations`.

    Each join overlaps by CROSSFADE_FRAMES, centered on the scene boundary.
    The overlap is made of cloned edge frames, so the wipe never falls
    through to black, and the picture still ends at exactly sum(durations).
    The wipe replaces one picture with the next. It does not blend them.
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
            f"[{current}][v{i}]xfade=transition={transition_for(i - 1)}"
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
        *h264_args(), "-an",
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


def has_audio_stream(path: str) -> bool:
    """True when the file actually contains playable audio."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and "audio" in (proc.stdout or "")


def build_music_mux_cmd(
    video: str, music: str, out: str, total_seconds: int,
    volume: float = 0.8, fade_out: int = 2, fade_in: float | None = None,
) -> list[str]:
    """Lay a soundtrack over finished video without re-encoding the picture.

    The audio is trimmed and then padded to the video length, and the output
    is capped at that same length. A short track cannot end the file early,
    and a long track cannot run past it.
    """
    if fade_in is None:
        fade_in = 1.0
    duration = float(total_seconds)
    span = f"{duration:.3f}"
    filters = [
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        f"volume={max(0.0, min(volume, 2.0)):.3f}",
        f"atrim=end={span}",
        "asetpts=PTS-STARTPTS",
    ]
    # Keep the fades from overlapping. A reel shorter than both is left alone.
    if fade_in > 0 and duration > fade_out + fade_in:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0 and duration > fade_out:
        filters.append(f"afade=t=out:st={duration - fade_out:.3f}:d={fade_out}")
    filters.extend(["apad", f"atrim=end={span}", "asetpts=PTS-STARTPTS"])
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video, "-i", music,
        "-filter_complex", "[1:a]" + ",".join(filters) + "[a]",
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-t", span,
        "-movflags", "+faststart", out,
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
        chain.append(_drawtext(caption, width, height, big=False, seconds=seconds))
    chain.append("format=yuv420p")

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += [
        "-vf", ",".join(chain),
        "-r", str(fps),
        *h264_args(), "-an",
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
