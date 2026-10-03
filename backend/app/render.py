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
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger("reelforge.render")

# Shown to customers. Names no provider, host, or file path.
CUSTOMER_RENDER_ERROR = (
    "Your reel couldn't be generated right now. Please try again."
)

from . import ffmpeg
from .ffmpeg import RenderError
from .providers import SceneSpec, VideoGenerator
from .export_presets import ExportPreset
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
            # The scene number is for the customer. The encoder text stays on
            # the failure record and in the log, not in this message.
            super().__init__(
                f"Scene {only.index + 1} couldn’t be generated. Please try again."
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
    fade_in: float = 1.0


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
    if provider == "mock":
        # Still motion follows the scene index. A moved photo cannot reuse
        # the clip from its old place. Other providers do not use that index.
        parts.append(str(spec.index))
        # The local still look is not part of the scene row. Without this, a
        # re-render would keep the previous Ken Burns clip. The lines are the
        # text actually drawn, so a disabled title cannot reuse an old clip.
        parts.append(ffmpeg.STILL_LOOK_ID)
        parts.append(spec.text_title or "")
        parts.append(spec.text_subtitle or "")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


_FORBIDDEN_OVERLAY = re.compile(
    r"\bno (?:text|logo|watermark|caption)\b", re.IGNORECASE
)


def prompt_forbids_overlay(prompt: str | None) -> bool:
    """True when the scene prompt says the picture must stay free of type."""
    return _FORBIDDEN_OVERLAY.search(prompt or "") is not None


def configured_end_card(scenes: list[dict]) -> tuple[str, str, int] | None:
    """A brand card after the last scene, only when that scene asks for one."""
    if not scenes:
        return None
    card = scenes[-1].get("end_card")
    if not isinstance(card, dict):
        return None
    title = str(card.get("title") or "").strip()
    subtitle = str(card.get("subtitle") or "").strip()
    if not title and not subtitle:
        return None
    try:
        seconds = int(card.get("seconds", 3))
    except (TypeError, ValueError):
        seconds = 3
    return title, subtitle, max(1, min(seconds, 8))


def visible_overlay(scene: dict) -> tuple[str | None, str | None]:
    """Title and subtitle to burn, or nothing when the customer left them off.

    The project idea is not a title. A scene with no design text stays a photograph.
    A prompt that forbids text, a logo, a watermark, or a caption stays a photograph
    even when titles are switched on. The brand card is a separate clip.
    """
    if prompt_forbids_overlay(scene.get("prompt")):
        return (None, None)
    title = (scene.get("text_title") or "").strip()
    subtitle = (scene.get("text_subtitle") or "").strip()
    if not scene.get("show_title"):
        title = ""
    if not scene.get("show_subtitle"):
        subtitle = ""
    return (title or None, subtitle or None)


def build_scene_spec(scene: dict, index: int, width: int, height: int,
                     image: Path | None, quality: str, fps: int = ffmpeg.FPS) -> SceneSpec:
    text_title, text_subtitle = visible_overlay(scene)
    return SceneSpec(
        scene_id=scene["id"],
        index=index,
        title=scene["title"],
        prompt=scene["prompt"],
        caption=scene.get("caption"),
        seconds=max(1, int(scene["duration"])),
        width=width,
        height=height,
        fps=fps,
        image_path=image,
        quality=quality,
        text_title=text_title,
        text_subtitle=text_subtitle,
    )


def assembly_joins(asset_ids: list[str | None]) -> list[str]:
    """How each pair of neighboring scenes is joined.

    The same asset is a hard cut. Different images, or scenes with no still,
    use the same wipe.
    """
    joins: list[str] = []
    for index in range(1, len(asset_ids)):
        left = asset_ids[index - 1]
        right = asset_ids[index]
        joins.append("cut" if left and left == right else "fade")
    return joins


def group_bounds(asset_ids: list[str | None]) -> list[tuple[int, int]]:
    """Half-open ranges of scenes that share one asset and must be cut together."""
    if not asset_ids:
        return []
    groups: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(asset_ids)):
        left = asset_ids[index - 1]
        right = asset_ids[index]
        if not (left and left == right):
            groups.append((start, index))
            start = index
    groups.append((start, len(asset_ids)))
    return groups


def assembly_stages(asset_ids: list[str | None]) -> list[str]:
    """FFmpeg stages for these assets: hard concat inside a group, fade between groups."""
    groups = group_bounds(asset_ids)
    if len(groups) <= 1:
        return ["concat"]
    stages: list[str] = []
    if any(end - start > 1 for start, end in groups):
        stages.append("concat")
    stages.append("crossfade")
    return stages


def _write_concat(work_dir: Path, name: str, paths: list[Path], out: Path) -> None:
    list_file = work_dir / f"{name}.txt"
    list_file.write_text(
        ffmpeg.build_concat_list([str(path) for path in paths]), encoding="utf-8"
    )
    ffmpeg.run(ffmpeg.build_concat_cmd(str(list_file), str(out)))


def assemble_timeline(
    clips: list[Path], scenes: list[dict], work_dir: Path, joined: Path,
) -> None:
    """Join scene clips. Identical neighboring assets are a cut; the rest wipe."""
    asset_ids = [scene.get("asset_id") for scene in scenes]
    groups = group_bounds(asset_ids)
    if len(groups) <= 1:
        _write_concat(work_dir, "concat", clips, joined)
        return
    group_paths: list[Path] = []
    group_durations: list[int] = []
    for index, (start, end) in enumerate(groups):
        members = clips[start:end]
        duration = sum(max(1, int(scenes[i]["duration"])) for i in range(start, end))
        if len(members) == 1:
            group_paths.append(members[0])
        else:
            merged = work_dir / f"cut_{index:02d}.mp4"
            _write_concat(work_dir, f"cut_{index:02d}", members, merged)
            group_paths.append(merged)
        group_durations.append(duration)
    ffmpeg.run(ffmpeg.build_crossfade_cmd(
        [str(path) for path in group_paths], group_durations, str(joined),
    ))


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
    preset: ExportPreset | None = None,
) -> Path:
    """Render `scenes` into a single mp4 at `out_path`. Returns that path."""
    if not ffmpeg.ffmpeg_available():
        raise RenderError("ffmpeg was not found on PATH")
    if not scenes:
        raise RenderError("nothing to render: this project has no scenes")
    if not generator.available():
        log.warning("provider %s is not available", generator.name)
        raise RenderError(CUSTOMER_RENDER_ERROR)

    hooks = hooks or NoHooks()
    if preset is not None:
        width, height, fps = preset.width, preset.height, preset.fps
    else:
        width, height = resolution_for(aspect_ratio)
        fps = ffmpeg.FPS
    work_dir.mkdir(parents=True, exist_ok=True)
    audio = audio or AudioSettings()

    clips: list[Path] = []
    failures: list[SceneFailure] = []

    try:
        for i, scene in enumerate(scenes):
            if hooks.should_cancel():
                raise RenderCancelled("cancelled before scene %d" % (i + 1))

            spec = build_scene_spec(
                scene, i, width, height, images.get(scene["id"]), quality, fps
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
                    log.warning(
                        "provider %s produced no clip for scene %s",
                        generator.name, spec.scene_id,
                    )
                    raise RenderError(CUSTOMER_RENDER_ERROR)
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

        out_path.parent.mkdir(parents=True, exist_ok=True)
        joined = work_dir / "joined.mp4"
        timeline_scenes = list(scenes)
        card = configured_end_card(scenes)
        if card is not None:
            title, subtitle, card_seconds = card
            caption = title if not subtitle else f"{title}  {subtitle}"
            card_path = work_dir / "end_card.mp4"
            ffmpeg.run(ffmpeg.build_card_clip_cmd(
                out=str(card_path), seconds=card_seconds,
                width=width, height=height, caption=caption, index=len(scenes),
                fps=fps,
            ))
            clips.append(card_path)
            timeline_scenes.append({
                "asset_id": "end-card",
                "duration": card_seconds,
            })
        assemble_timeline(clips, timeline_scenes, work_dir, joined)
        if on_progress:
            on_progress(90)

        total = sum(int(s["duration"]) for s in timeline_scenes)
        music = audio.music_path
        usable = (
            music is not None
            and Path(music).is_file()
            and ffmpeg.has_audio_stream(str(music))
        )
        if music is not None and not usable:
            log.info("music left off the reel; the picture is unchanged")
        if usable:
            ffmpeg.run(ffmpeg.build_music_mux_cmd(
                video=str(joined), music=str(music),
                out=str(out_path), total_seconds=total,
                volume=audio.volume, fade_out=audio.fade_out,
                fade_in=audio.fade_in,
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
