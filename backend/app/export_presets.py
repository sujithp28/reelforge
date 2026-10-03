"""Publish formats.

A customer picks a destination. The width, height, frame rate, and encoder
requirements live here, and the renderer receives this object. FFmpeg filters
never see a platform name.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ffmpeg import FPS
from .storyboard import ASPECT_SIZES, resolution_for


@dataclass(frozen=True)
class ExportPreset:
    id: str
    label: str
    aspect_ratio: str
    width: int
    height: int
    fps: int
    video_codec: str = "libx264"
    pix_fmt: str = "yuv420p"


# Customer-facing labels. Do not put resolution or codec names in `label`.
PRESETS: dict[str, ExportPreset] = {
    "instagram_reel": ExportPreset(
        "instagram_reel", "Instagram Reel", "9:16", 1080, 1920, FPS,
    ),
    "youtube_short": ExportPreset(
        "youtube_short", "YouTube Short", "9:16", 1080, 1920, FPS,
    ),
    "instagram_feed": ExportPreset(
        "instagram_feed", "Instagram Feed", "4:5", 1080, 1350, FPS,
    ),
    "youtube_landscape": ExportPreset(
        "youtube_landscape", "YouTube", "16:9", 1920, 1080, FPS,
    ),
}


def get_preset(preset_id: str | None) -> ExportPreset | None:
    if not preset_id:
        return None
    return PRESETS.get(preset_id)


def resolve(export_preset: str | None, aspect_ratio: str) -> ExportPreset:
    """The preset to render.

    A stored preset wins. A project created before presets existed keeps the
    frame its aspect ratio already used, including square.
    """
    chosen = get_preset(export_preset)
    if chosen is not None:
        return chosen
    width, height = resolution_for(aspect_ratio)
    ratio = aspect_ratio if aspect_ratio in ASPECT_SIZES else "9:16"
    return ExportPreset(
        id="",
        label="",
        aspect_ratio=ratio,
        width=width,
        height=height,
        fps=FPS,
    )
