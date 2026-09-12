"""Video assembly.

Asks a VideoGenerator provider for one clip per scene, then uses ffmpeg to
join them, add the soundtrack, and produce the finished reel. Provider-neutral
by construction: it only ever sees clip files on disk.

Scene isolation is the other job of this module. Generation can fail for one
scene while the rest are fine, and on a billable provider re-running the
successful scenes is wasted money. So each scene's clip is fingerprinted and
cached through `RenderHooks`; a scene whose inputs have not changed is reused
rather than regenerated. render.py itself touches neither the database nor
storage — the hooks do, which keeps the layering one-way.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import ffmpeg
from .ffmpeg import RenderError
from .providers import SceneSpec, VideoGenerator
from .storyboard import resolution_for


class RenderCancelled(RuntimeError):
    """Raised when a cancellation was requested between scenes."""


@dataclass
class SceneFailure:
    scene_id: str
    index: int
    title: str
    message: str


class SceneGenerationError(RenderError):
    """One or more scenes could not be generated.

    Carries the per-scene detail so the caller can tell the customer which
    scene to retry instead of failing the whole reel anonymously.
    """

    def __init__(self, failures: list[SceneFailure], total: int):
        self.failures = failures
        self.total = total
        if len(failures) == 1:
            only = failures[0]
            super().__init__(
                f"Scene {only.index + 1} couldn’t be generated. {only.message}"
            )
        else:
            listed = ", ".join(str(f.index + 1) for f in failures)
            super().__init__(
                f"{len(failures)} of {total} scenes couldn’t be generated"
                f" (scenes {listed})."
            )


@dataclass
class AudioSettings:
    music_path: Path | None = None
    volume: float = 0.8
    fade_out: int = 2


class RenderHooks(Protocol):
    """Persistence and control surface supplied by the job layer."""

    def should_cancel(self) -> bool: ...
    def cached_clip(self, scene_id: str, fingerprint: str) -> Path | None: ...
    def clip_succeeded(self, scene_id: str, fingerprint: str, clip: Path) -> None: ...
    def clip_failed(self, scene_id: str, fingerprint: str, message: str) -> None: ...
    def generation_logged(
        self, scene_id: str, status: str, seconds: int, elapsed_ms: int,
        error: str | None,
    ) -> None: ...


@dataclass
class NoHooks:
    """Default: generate everything, cache nothing, never cancel."""

    calls: list[tuple] = field(default_factory=list)

    def should_cancel(self) -> bool:
        return False

    def cached_clip(self, scene_id: str, fingerprint: str) -> Path | None:
        return None

    def clip_succeeded(self, scene_id: str, fingerprint: str, clip: Path) -> None:
        self.calls.append(("ok", scene_id))

    def clip_failed(self, scene_id: str, fingerprint: str, message: str) -> None:
        self.calls.append(("fail", scene_id))

    def generation_logged(
        self, scene_id: str, status: str, seconds: int, elapsed_ms: int,
        error: str | None,
    ) -> None:
        self.calls.append(("log", scene_id, status))


def scene_fingerprint(spec: SceneSpec, provider: str) -> str:
    """Identity of a scene's generated output.

    Any change that would alter the pixels must change this, or a stale clip
    is silently reused. Anything that would not must leave it alone, or the
    cache never hits and every render pays again.
    """
    parts = [
        provider,
        spec.prompt,
        spec.caption or "",
        str(spec.seconds),
        f"{spec.width}x{spec.height}",
        str(spec.fps),
        spec.quality,
        # The asset id is part of an uploaded file's name, so a replaced
        # reference image produces a different name and a different clip.
        Path(spec.image_path).name if spec.image_path else "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def build_scene_spec(scene: dict, index: int, width: int, height: int,
                     image: Path | None, quality: str) -> SceneSpec:
    return SceneSpec(
        scene_id=scene["id"],
        index=index,
        title=scene["title"],
        prompt=scene["prompt"],
        caption=scene.get("caption"),
        seconds=max(1, int(scene["duration"])),
        width=width,
        height=height,
        image_path=image,
        quality=quality,
    )


def render_reel(
    scenes: list[dict],
    images: dict[str, Path | None],
    aspect_ratio: str,
    work_dir: Path,
    out_path: Path,
    generator: VideoGenerator,
    audio: AudioSettings | None = None,
    on_progress: Callable[[int], None] | None = None,
    hooks: RenderHooks | None = None,
    quality: str = "standard",
) -> Path:
    """Render `scenes` into a single mp4 at `out_path`. Returns that path."""
    if not ffmpeg.ffmpeg_available():
        raise RenderError("ffmpeg was not found on PATH")
    if not scenes:
        raise RenderError("nothing to render: this project has no scenes")
    if not generator.available():
        raise RenderError(
            f"the {generator.name!r} video provider is not available;"
            " check its configuration"
        )

    hooks = hooks or NoHooks()
    width, height = resolution_for(aspect_ratio)
    work_dir.mkdir(parents=True, exist_ok=True)
    audio = audio or AudioSettings()

    clips: list[Path] = []
    failures: list[SceneFailure] = []

    try:
        for i, scene in enumerate(scenes):
            if hooks.should_cancel():
                raise RenderCancelled("cancelled before scene %d" % (i + 1))

            spec = build_scene_spec(
                scene, i, width, height, images.get(scene["id"]), quality
            )
            fingerprint = scene_fingerprint(spec, generator.name)

            reused = hooks.cached_clip(scene["id"], fingerprint)
            if reused is not None:
                clips.append(reused)
                if on_progress:
                    on_progress(int(5 + 80 * (i + 1) / len(scenes)))
                continue

            clip = work_dir / f"scene_{i:02d}.mp4"
            started = time.monotonic()
            try:
                generator.generate(spec, clip)
                if not clip.exists() or clip.stat().st_size == 0:
                    raise RenderError(
                        f"the {generator.name!r} provider produced no clip"
                    )
            except RenderError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                message = str(exc)
                hooks.clip_failed(scene["id"], fingerprint, message)
                hooks.generation_logged(
                    scene["id"], "failed", spec.seconds, elapsed, message
                )
                # Keep going: the other scenes are still worth having, and a
                # retry should only pay for this one.
                failures.append(SceneFailure(scene["id"], i, scene["title"], message))
                continue

            elapsed = int((time.monotonic() - started) * 1000)
            hooks.clip_succeeded(scene["id"], fingerprint, clip)
            hooks.generation_logged(
                scene["id"], "succeeded", spec.seconds, elapsed, None
            )
            clips.append(clip)
            if on_progress:
                # Leave the last 15% for concatenation and the audio mux.
                on_progress(int(5 + 80 * (i + 1) / len(scenes)))

        if failures:
            raise SceneGenerationError(failures, len(scenes))

        if hooks.should_cancel():
            raise RenderCancelled("cancelled before assembly")

        list_file = work_dir / "concat.txt"
        list_file.write_text(
            ffmpeg.build_concat_list([str(c) for c in clips]), encoding="utf-8"
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        joined = work_dir / "joined.mp4"
        ffmpeg.run(ffmpeg.build_concat_cmd(str(list_file), str(joined)))
        if on_progress:
            on_progress(90)

        total = sum(int(s["duration"]) for s in scenes)
        if audio.music_path and Path(audio.music_path).exists():
            ffmpeg.run(ffmpeg.build_music_mux_cmd(
                video=str(joined), music=str(audio.music_path),
                out=str(out_path), total_seconds=total,
                volume=audio.volume, fade_out=audio.fade_out,
            ))
        else:
            ffmpeg.run(ffmpeg.build_finalize_cmd(str(joined), str(out_path)))

        if on_progress:
            on_progress(100)
        return out_path
    finally:
        # `keep` matters: a caller may legitimately stage the output inside the
        # work directory, and deleting the finished reel here would be silent.
        cleanup(work_dir, keep=out_path)


def cleanup(work_dir: Path, keep: Path | None = None) -> None:
    """Remove intermediates. Never raises — the reel itself is already done."""
    if not work_dir.exists():
        return
    spared = keep.resolve() if keep else None
    for child in sorted(work_dir.rglob("*"), reverse=True):
        try:
            if child.is_file():
                if spared is None or child.resolve() != spared:
                    child.unlink(missing_ok=True)
            else:
                child.rmdir()
        except OSError:
            pass
    try:
        work_dir.rmdir()
    except OSError:
        # Not empty, because the output is still staged inside it.
        pass
