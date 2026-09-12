"""VideoGenerator providers.

A provider turns one scene into one video clip on disk. Everything downstream
(concatenation, captions, music, progress, status) lives in render.py and does
not care which provider produced the clips — that is the point of the seam.

  mock  the working local renderer: a Ken Burns move over an uploaded still,
        or a typographic colour card when a scene has no still. No GPU, no
        network, no credentials. Remains the default.
  ltx   real AI generation through the LTX hosted API. Implemented in ltx.py;
        reports itself unavailable until an endpoint and key are configured.
        Paid, and kept available as an option.
  kaggle real AI generation on a free Kaggle GPU. Implemented in kaggle.py:
        the backend queues scene jobs and an external worker drains them.
        The intended provider for the beta.

Providers must honour the requested duration and resolution exactly. render.py
stream-copies clips during concatenation, so a clip that disagrees about
resolution or frame rate corrupts the join instead of failing loudly. A
provider whose model cannot hit those numbers is responsible for closing the
gap itself, rather than relaxing the contract.
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
    # Customer-facing quality only: "standard" or "high". Providers map this
    # to their own settings; no model-specific parameter reaches this struct.
    quality: str = "standard"



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


# The real providers live in ltx.py and kaggle.py. They are imported lazily
# inside build_generator() because both import SceneSpec from this module, and
# a module-level import here would be circular.


def build_generator(name: str | None = None) -> VideoGenerator:
    chosen = (name or config.VIDEO_PROVIDER).lower()
    if chosen == "mock":
        return MockGenerator()
    if chosen == "ltx":
        from .ltx import LTXGenerator

        return LTXGenerator(
            endpoint=config.LTX_ENDPOINT,
            api_key=config.LTX_API_KEY,
            timeout=config.LTX_TIMEOUT_SECONDS,
        )
    if chosen == "kaggle":
        from .kaggle import KaggleGenerator

        return KaggleGenerator(
            worker_token=config.KAGGLE_WORKER_TOKEN,
            job_timeout=config.KAGGLE_JOB_TIMEOUT_SECONDS,
            poll_seconds=config.KAGGLE_POLL_SECONDS,
        )
    raise RenderError(
        f"unknown video provider {chosen!r};"
        f" expected one of: {', '.join(PROVIDER_NAMES)}"
    )


PROVIDER_NAMES = ("mock", "ltx", "kaggle")


def available_providers() -> dict[str, bool]:
    """Provider name to whether it can run, for /health and the API."""
    out = {}
    for name in PROVIDER_NAMES:
        try:
            out[name] = build_generator(name).available()
        except RenderError:
            out[name] = False
    return out


def configuration_error(name: str) -> str | None:
    """Operator-facing reason a provider cannot run, or None if it can."""
    try:
        generator = build_generator(name)
    except RenderError as exc:
        return str(exc)
    if generator.available():
        return None
    reporter = getattr(generator, "configuration_error", None)
    return reporter() if reporter else f"{name} is not configured"
