"""Video assembly.

Asks a VideoGenerator provider for one clip per scene, then uses ffmpeg to
join them, add the soundtrack, and produce the finished reel. Provider-neutral
by construction: it only ever sees clip files on disk.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ffmpeg
from .ffmpeg import RenderError
from .providers import SceneSpec, VideoGenerator
from .storyboard import resolution_for


@dataclass
class AudioSettings:
    music_path: Path | None = None
    volume: float = 0.8
    fade_out: int = 2


def render_reel(
    scenes: list[dict],
    images: dict[str, Path | None],
    aspect_ratio: str,
    work_dir: Path,
    out_path: Path,
    generator: VideoGenerator,
    audio: AudioSettings | None = None,
    on_progress: Callable[[int], None] | None = None,
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

    width, height = resolution_for(aspect_ratio)
    work_dir.mkdir(parents=True, exist_ok=True)
    audio = audio or AudioSettings()
    clips: list[Path] = []

    try:
        for i, scene in enumerate(scenes):
            clip = work_dir / f"scene_{i:02d}.mp4"
            spec = SceneSpec(
                scene_id=scene["id"],
                index=i,
                title=scene["title"],
                prompt=scene["prompt"],
                caption=scene.get("caption"),
                seconds=max(1, int(scene["duration"])),
                width=width,
                height=height,
                image_path=images.get(scene["id"]),
            )
            generator.generate(spec, clip)
            if not clip.exists():
                raise RenderError(
                    f"the {generator.name!r} provider did not produce a clip"
                    f" for scene {i + 1}"
                )
            clips.append(clip)
            if on_progress:
                # Leave the last 15% for concatenation and the audio mux.
                on_progress(int(5 + 80 * (i + 1) / len(scenes)))

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
