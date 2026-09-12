"""VideoGenerator providers.

A provider turns one scene into one video clip on disk. Everything downstream
(concatenation, captions, music, progress, status) lives in render.py and does
not care which provider produced the clips — that is the point of the seam.

  mock  the working local renderer: a Ken Burns move over an uploaded still,
        or a typographic colour card when a scene has no still. No GPU.
  ltx   integration boundary for LTX-Video. Declared and selectable, but
        refuses to run until an endpoint is configured.

Providers must honour the requested duration and resolution exactly. render.py
stream-copies clips during concatenation, so a clip that disagrees about
resolution or frame rate corrupts the join instead of failing loudly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import config
from .ffmpeg import FPS, RenderError, build_card_clip_cmd, build_still_clip_cmd, run


@dataclass(frozen=True)
class SceneSpec:
    """Everything a provider needs to render one scene."""
    scene_id: str
    index: int
    title: str
    prompt: str
    caption: str | None
    seconds: int
    width: int
    height: int
    fps: int = FPS
    image_path: Path | None = None


class VideoGenerator(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this provider can run right now."""
        ...

    def generate(self, spec: SceneSpec, out: Path) -> None:
        """Write a clip to `out` at exactly spec.seconds and spec resolution."""
        ...


class MockGenerator:
    """Local ffmpeg renderer. Deliberately kept as the working default."""

    name = "mock"

    def available(self) -> bool:
        return True

    def generate(self, spec: SceneSpec, out: Path) -> None:
        if spec.image_path and Path(spec.image_path).exists():
            cmd = build_still_clip_cmd(
                image=str(spec.image_path), out=str(out), seconds=spec.seconds,
                width=spec.width, height=spec.height, caption=spec.caption,
            )
        else:
            cmd = build_card_clip_cmd(
                out=str(out), seconds=spec.seconds, width=spec.width,
                height=spec.height, caption=spec.caption or spec.title,
                index=spec.index,
            )
        run(cmd)


class LTXGenerator:
    """LTX-Video provider — integration boundary, no GPU required yet.

    Wiring this up means implementing `generate` to submit
    `spec.prompt` plus the optional `spec.image_path` to the LTX endpoint,
    poll until the clip is done, and write it to `out` at spec.seconds and
    spec resolution. Nothing else in the app changes.

    Until REELFORGE_LTX_ENDPOINT is set, `available()` is False and selecting
    this provider is rejected at the API boundary rather than failing halfway
    through a render.
    """

    name = "ltx"

    def __init__(self, endpoint: str = "", api_key: str = "", timeout: int = 600):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.endpoint)

    def generate(self, spec: SceneSpec, out: Path) -> None:
        raise RenderError(
            "the LTX provider is an integration boundary and is not implemented"
            " yet. Set REELFORGE_VIDEO_PROVIDER=mock to render locally."
        )


def build_generator(name: str | None = None) -> VideoGenerator:
    chosen = (name or config.VIDEO_PROVIDER).lower()
    if chosen == "mock":
        return MockGenerator()
    if chosen == "ltx":
        return LTXGenerator(
            endpoint=config.LTX_ENDPOINT,
            api_key=config.LTX_API_KEY,
            timeout=config.LTX_TIMEOUT_SECONDS,
        )
    raise RenderError(
        f"unknown video provider {chosen!r}; expected one of: mock, ltx"
    )


def available_providers() -> dict[str, bool]:
    """Provider name to whether it can run, for /health and the API."""
    out = {}
    for name in ("mock", "ltx"):
        try:
            out[name] = build_generator(name).available()
        except RenderError:
            out[name] = False
    return out
