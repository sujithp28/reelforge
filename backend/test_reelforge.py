"""Self-check for the parts with real logic: duration maths and ffmpeg command
building. Plain asserts, no test framework.

    python test_reelforge.py
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Set before anything imports app.config. The image-reel test renders through
# the real upload and job path, and config reads these once at import.
_IMAGE_REEL_ROOT = Path(tempfile.mkdtemp(prefix="reelforge-image-reel-"))
os.environ["REELFORGE_DATA_DIR"] = str(_IMAGE_REEL_ROOT)
os.environ["REELFORGE_JOB_RUNNER"] = "external"
os.environ["REELFORGE_VIDEO_PROVIDER"] = "mock"
os.environ["REELFORGE_STORAGE"] = "local"

from app import ffmpeg, storyboard


def test_split_duration_sums_exactly():
    for total in range(6, 121):
        for weights in ([1.0, 1.0, 1.0], [1.1, 1.3, 0.9, 1.0, 0.9], [0.7] * 8):
            if total < len(weights):
                continue  # impossible by construction; covered below
            parts = storyboard.split_duration(total, weights)
            assert sum(parts) == total, (total, weights, parts)
            assert len(parts) == len(weights)
            assert all(p >= 1 for p in parts), parts


def test_split_duration_rejects_impossible_requests():
    # A 0-second clip kills the whole ffmpeg render, so refuse rather than emit one.
    try:
        storyboard.split_duration(6, [1.0] * 8)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for 8 scenes in 6 seconds")


def test_scene_count_never_exceeds_seconds():
    for duration in range(1, 121):
        assert storyboard.pick_scene_count(duration) <= duration, duration
        assert storyboard.pick_scene_count(duration) >= 1


def test_split_duration_respects_minimum_when_there_is_room():
    parts = storyboard.split_duration(30, [1.1, 1.3, 0.9, 1.0, 0.9])
    assert min(parts) >= storyboard.MIN_SCENE_SECONDS, parts
    # Heavier weights get more time.
    assert parts[1] >= parts[4], parts


def test_split_duration_degrades_gracefully_when_cramped():
    # 5 scenes into 5 seconds cannot honour a 2-second minimum.
    parts = storyboard.split_duration(5, [1.0] * 5)
    assert parts == [1, 1, 1, 1, 1], parts


def test_plan_scenes_matches_requested_duration():
    for duration in (10, 15, 30, 45, 60, 90):
        for category in list(storyboard.BEAT_SHEETS) + ["Other"]:
            scenes = storyboard.plan_scenes("a red bicycle", category, duration)
            assert sum(s["duration"] for s in scenes) == duration, (category, duration)
            assert storyboard.MIN_SCENES <= len(scenes) <= storyboard.MAX_SCENES
            assert [s["position"] for s in scenes] == list(range(len(scenes)))
            assert all(s["title"] and s["prompt"] for s in scenes)
            # The idea must actually reach the storyboard.
            assert any("red bicycle" in s["prompt"] for s in scenes), category


def test_plan_scenes_keeps_opening_and_closing_beats():
    short = storyboard.plan_scenes("x", "Cinematic", 12)
    sheet = storyboard.BEAT_SHEETS["Cinematic"]
    assert short[0]["title"] == sheet[0][0]
    assert short[-1]["title"] == sheet[-1][0]


def test_unknown_category_falls_back():
    scenes = storyboard.plan_scenes("x", "Definitely Not A Category", 30)
    assert sum(s["duration"] for s in scenes) == 30
    assert scenes[0]["title"] == storyboard.DEFAULT_SHEET[0][0]


def test_reprompt_varies_between_attempts():
    a = storyboard.reprompt_scene("x", "Product", "In use", 0)
    b = storyboard.reprompt_scene("x", "Product", "In use", 1)
    assert a != b
    assert "In use" not in a  # it returns a prompt, not a title


def test_resolution_for():
    assert storyboard.resolution_for("9:16") == (1080, 1920)
    assert storyboard.resolution_for("16:9") == (1920, 1080)
    assert storyboard.resolution_for("nonsense") == (1080, 1920)


def test_drawtext_escaping():
    # Unescaped colons and percents break the whole ffmpeg filtergraph.
    out = ffmpeg.escape_drawtext("Price: 20% off, it's [real]\nsecond line")
    for ch in (":", "%", ",", "[", "]"):
        assert "\\" + ch in out, (ch, out)
    # Apostrophes cannot be escaped inside a single-quoted value at all.
    assert "'" not in out
    assert "’" in out
    assert "\n" not in out


def test_font_is_discoverable():
    # Captions are impossible without an explicit fontfile where fontconfig
    # is absent, which includes a default Windows install.
    assert ffmpeg.find_font(), "no font found; set REELFORGE_FONT"
    arg = ffmpeg._font_arg()
    assert arg.startswith("fontfile='") and arg.endswith("':"), arg
    # Every colon in the path must be escaped or ffmpeg reads it as the
    # separator before the next filter option. A Windows path has one from
    # the drive letter; a POSIX path has none, so check the invariant rather
    # than assuming either platform.
    inner = arg[len("fontfile='"):-len("':")]
    assert ":" not in inner.replace("\\:", ""), inner


def test_still_clip_command_is_wellformed():
    cmd = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=7, width=1080, height=1920, caption="Hi"
    )
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd and "in.jpg" in cmd and cmd[-1] == "out.mp4"
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1080:1920" in vf and "drawtext" in vf and "crop=w=" in vf
    # zoompan holds the first step on a still, so the move is an animated crop.
    assert "zoompan" not in vf, vf
    assert "force_original_aspect_ratio=increase" in vf
    assert "-t" in cmd


def test_still_motion_follows_the_scene_index():
    filters = []
    for index, size in ((0, (1080, 1920)), (1, (1920, 1080)), (2, (1080, 1080)), (3, (1080, 1350)), (4, (1080, 1920))):
        width, height = size
        cmd = ffmpeg.build_still_clip_cmd(
            image="in.jpg", out="out.mp4", seconds=6,
            width=width, height=height, caption=None, index=index,
        )
        vf = cmd[cmd.index("-vf") + 1]
        assert f"scale={width}:{height}:flags=lanczos" in vf
        filters.append(vf)
    assert len(set(filters)) == 5, "each scene index must take a different path"


def test_crossfade_command_dissolves_without_a_dip_to_black():
    cmd = ffmpeg.build_crossfade_cmd(["a.mp4", "b.mp4", "c.mp4"], [6, 7, 5], "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("xfade=transition=fade:") == 2
    assert "fadeblack" not in graph
    assert "tpad=" in graph
    assert cmd[cmd.index("-frames:v") + 1] == str(18 * ffmpeg.FPS)
    assert cmd[cmd.index("-r") + 1] == str(ffmpeg.FPS)


def test_card_clip_command_has_no_input_file():
    cmd = ffmpeg.build_card_clip_cmd(
        out="out.mp4", seconds=4, width=1080, height=1920, caption="Closing", index=2
    )
    assert "lavfi" in cmd
    assert cmd[-1] == "out.mp4"


def _media(path: Path, seconds: int, audio: bool) -> None:
    if audio:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(seconds), str(path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=320x180:d={seconds}:r=30",
            "-t", str(seconds), str(path),
        ]
    ffmpeg.run(cmd)


def test_soundtrack_length_cannot_change_the_reel():
    """30s picture stays 30s with a short track, a long track, or no track."""
    if not ffmpeg.ffmpeg_available():
        print("  (skipped soundtrack duration: ffmpeg not on PATH)")
        return
    root = Path(tempfile.mkdtemp(prefix="reelforge-mux-"))
    try:
        video = root / "video.mp4"
        short = root / "short.wav"
        long = root / "long.wav"
        _media(video, 30, audio=False)
        _media(short, 12, audio=True)
        _media(long, 45, audio=True)
        cases = (("short", short), ("long", long), ("none", None))
        for name, music in cases:
            out = root / f"{name}.mp4"
            if music is None:
                ffmpeg.run(ffmpeg.build_finalize_cmd(str(video), str(out)))
            else:
                ffmpeg.run(ffmpeg.build_music_mux_cmd(
                    str(video), str(music), str(out),
                    total_seconds=30, volume=0.8, fade_out=2,
                ))
            duration = ffmpeg.probe_duration(str(out))
            assert duration is not None and abs(duration - 30) < 0.15, (name, duration)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_unavailable_provider_message_names_nothing():
    if not ffmpeg.ffmpeg_available():
        print("  (skipped provider message: ffmpeg not on PATH)")
        return
    from app import render

    class Hidden:
        name = "wan"

        def available(self) -> bool:
            return False

        def generate(self, spec, out) -> None:
            raise AssertionError("unavailable provider was asked to generate")

    root = Path(tempfile.mkdtemp(prefix="reelforge-hidden-"))
    try:
        render.render_reel(
            scenes=[{"id": "s", "title": "Opening", "prompt": "p", "duration": 2,
                     "caption": None}],
            images={}, aspect_ratio="9:16",
            work_dir=root / "work", out_path=root / "out.mp4",
            generator=Hidden(),
        )
    except render.RenderError as exc:
        text = str(exc).lower()
        for leak in ("wan", "kaggle", "ltx", "mock"):
            assert leak not in text, text
        assert "couldn't be generated" in text
    else:
        raise AssertionError("an unavailable provider was allowed to render")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_concat_file_escapes_quotes():
    body = ffmpeg.build_concat_list(["a b.mp4", "it's.mp4"])
    assert "'a b.mp4'" in body
    assert "it'\\''s.mp4" in body or "it\\'s.mp4" in body, body


def test_crossfade_keeps_duration_fps_ratio_and_each_image():
    """A dissolve between stills must not shorten the reel or mix up the photos."""
    if not ffmpeg.ffmpeg_available():
        print("  (skipped crossfade reel: ffmpeg not on PATH)")
        return

    from app import render
    from app.providers import MockGenerator

    root = Path(tempfile.mkdtemp(prefix="reelforge-fade-"))
    try:
        colors = ("0xE23B2F", "0x1D4ED8", "0x16A34A")
        images: dict[str, Path] = {}
        scenes = []
        for index, color in enumerate(colors):
            still = root / f"room-{index}.jpg"
            ffmpeg.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c={color}:s=1280x720",
                "-frames:v", "1", str(still),
            ])
            scene_id = f"s{index}"
            images[scene_id] = still
            scenes.append({
                "id": scene_id, "title": f"Room {index}", "prompt": "p",
                "duration": 2, "caption": None,
            })
        out = root / "reel.mp4"
        render.render_reel(
            scenes=scenes, images=images, aspect_ratio="9:16",
            work_dir=root / "work", out_path=out, generator=MockGenerator(),
        )
        duration = ffmpeg.probe_duration(str(out))
        assert duration is not None and abs(duration - 6) < 0.02, duration
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert info == "1080,1920,30/1", info
        red = _rgb(out, 0.4, "120:120:80:80")
        blue = _rgb(out, 2.4, "120:120:80:80")
        green = _rgb(out, 4.4, "120:120:80:80")
        assert red[0] > 140 and red[0] > red[2] + 40, red
        assert blue[2] > 140 and blue[2] > blue[0] + 40, blue
        assert green[1] > 100 and green[1] > green[0] + 40, green
        # The join at 2.0s is the middle of the dissolve, so both rooms show.
        mix = _rgb(out, 2.0, "120:120:80:80")
        assert mix[0] > 50 and mix[2] > 50, mix
        assert abs(mix[0] - red[0]) > 40 and abs(mix[2] - blue[2]) > 40, mix
        assert sum(mix) > 80, mix

        wide_scenes = [
            {"id": "w0", "title": "A", "prompt": "p", "duration": 2, "caption": None},
            {"id": "w1", "title": "B", "prompt": "p", "duration": 2, "caption": None},
        ]
        wide_images = {"w0": images["s0"], "w1": images["s1"]}
        for ratio, expect in (("16:9", "1920,1080,30/1"), ("1:1", "1080,1080,30/1"), ("4:5", "1080,1350,30/1")):
            wide = root / f"{ratio.replace(':', 'x')}.mp4"
            render.render_reel(
                scenes=wide_scenes, images=wide_images, aspect_ratio=ratio,
                work_dir=root / f"work-{ratio.replace(':', 'x')}", out_path=wide,
                generator=MockGenerator(),
            )
            wide_duration = ffmpeg.probe_duration(str(wide))
            assert wide_duration is not None and abs(wide_duration - 4) < 0.02, (ratio, wide_duration)
            wide_info = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate",
                 "-of", "csv=p=0", str(wide)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert wide_info == expect, (ratio, wide_info)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _rgb(video: Path, at: float, crop: str) -> tuple[float, float, float]:
    """Average colour of a crop at `at` seconds. Used to tell a photo from a card."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(video),
            "-frames:v", "1", "-vf", f"crop={crop},scale=8:8",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True, check=True,
    )
    data = proc.stdout
    pixels = len(data) // 3
    assert pixels > 0, "frame sample was empty"
    return (
        sum(data[i] for i in range(0, len(data), 3)) / pixels,
        sum(data[i + 1] for i in range(0, len(data), 3)) / pixels,
        sum(data[i + 2] for i in range(0, len(data), 3)) / pixels,
    )


def _frame_delta(video: Path, a: float, b: float) -> float:
    """Mean absolute RGB difference between two frames. A static card is ~0."""
    def raw(at: float) -> bytes:
        proc = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(video),
                "-frames:v", "1", "-vf", "scale=48:86",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            capture_output=True, check=True,
        )
        return proc.stdout

    left, right = raw(a), raw(b)
    n = min(len(left), len(right))
    assert n > 0
    return sum(abs(left[i] - right[i]) for i in range(n)) / n


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.removeprefix("0x")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def test_browser_create_upload_render_puts_the_image_in_the_mp4():
    """Same requests the create page sends: JSON project, then multipart upload.

    The photo is not a scene field on create. It is a second request with no
    scene id, which must attach to every bare scene. The finished MP4 has to
    show that image in every scene. A title card anywhere is a failure.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped browser image flow: ffmpeg not on PATH)")
        return

    from fastapi.testclient import TestClient

    from app import jobs
    from app.main import app

    root = _IMAGE_REEL_ROOT
    client = TestClient(app)
    created = client.post("/api/projects", json={
        "idea": "a modern luxury living room",
        "category": "Cinematic",
        "input_type": "Idea",
        "duration": 6,
        "aspect_ratio": "9:16",
    })
    assert created.status_code == 201, created.text
    project = created.json()
    assert all(s["asset_url"] is None for s in project["scenes"])

    still = root / "living-room.jpg"
    ffmpeg.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0xE23B2F:s=1280x720",
        "-frames:v", "1", str(still),
    ])
    uploaded = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("living-room.jpg", still.read_bytes(), "image/jpeg")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["assets"] and body["assets"][0]["kind"] == "image"
    assert all(s["asset_url"] for s in body["scenes"]), body["scenes"]
    scenes = body["scenes"]
    assert sum(s["duration"] for s in scenes) == 6

    started = client.post(f"/api/projects/{project['id']}/render", json={})
    assert started.status_code == 202, started.text
    jobs._execute(started.json()["job_id"], project["id"])

    done = client.get(f"/api/projects/{project['id']}").json()
    assert done["status"] == "ready", done.get("error")
    video = root / done["video_url"].split("/media/", 1)[1]
    duration = ffmpeg.probe_duration(str(video))
    assert duration is not None and abs(duration - 6) < 0.05, duration
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert info == "1080,1920,30/1", info

    print(
        f"  browser-flow mp4 {video.name} {video.stat().st_size} bytes"
        f" {duration:.3f}s {info}"
    )
    cursor = 0.4
    for scene in scenes:
        pixel = _rgb(video, cursor, "120:120:80:80")
        print(f"  frame {cursor:.1f}s {scene['title']} rgb={tuple(round(c) for c in pixel)}")
        assert pixel[0] > 140 and pixel[0] > pixel[2] + 40, (scene["title"], pixel)
        for color in ffmpeg.CARD_COLORS:
            card = _hex_rgb(color)
            distance = sum(abs(pixel[i] - card[i]) for i in range(3))
            assert distance > 80, (scene["title"], pixel, color)
        cursor += scene["duration"]


def test_product_image_source_upload_is_on_every_scene():
    """Customer path: category Product, source Image, then the photo.

    The create body is JSON and has no file. The photo is the next request,
    with no scene id, and every scene must show that image. Title-card pixels
    are a failure.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped product image flow: ffmpeg not on PATH)")
        return

    from fastapi.testclient import TestClient

    from app import jobs
    from app.main import app

    root = _IMAGE_REEL_ROOT
    client = TestClient(app)
    created = client.post("/api/projects", json={
        "idea": "a teal bottle on a white table",
        "category": "Product",
        "input_type": "Image",
        "duration": 6,
        "aspect_ratio": "9:16",
    })
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["input_type"] == "Image"
    assert project["category"] == "Product"
    assert all(s["asset_url"] is None for s in project["scenes"])

    still = root / "product-bottle.jpg"
    ffmpeg.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x0F766E:s=1280x720",
        "-frames:v", "1", str(still),
    ])
    uploaded = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("product-bottle.jpg", still.read_bytes(), "image/jpeg")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    asset_ids = {a["id"] for a in body["assets"] if a["kind"] == "image"}
    assert len(asset_ids) == 1
    assert all(s["asset_id"] in asset_ids and s["asset_url"] for s in body["scenes"])

    started = client.post(f"/api/projects/{project['id']}/render", json={})
    assert started.status_code == 202, started.text
    jobs._execute(started.json()["job_id"], project["id"])

    done = client.get(f"/api/projects/{project['id']}").json()
    assert done["status"] == "ready", done.get("error")
    video = root / done["video_url"].split("/media/", 1)[1]
    cursor = 0.4
    for scene in body["scenes"]:
        pixel = _rgb(video, cursor, "120:120:80:80")
        assert pixel[1] > 80 and pixel[1] > pixel[0] + 40, (scene["title"], pixel)
        for color in ffmpeg.CARD_COLORS:
            card = _hex_rgb(color)
            distance = sum(abs(pixel[i] - card[i]) for i in range(3))
            assert distance > 80, (scene["title"], pixel, color)
        cursor += scene["duration"]


def test_four_scene_images_are_different_frames_in_the_mp4():
    """Four photos on a 30s cinematic reel. The last photo also closes the reel.

    Scene clips and the finished MP4 are both sampled. Database asset ids are
    not enough: the pixels at 1s, 8s, 14s, 20s and 26s have to change with the
    assigned photo.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped multi-image reel: ffmpeg not on PATH)")
        return

    from fastapi.testclient import TestClient

    from app import jobs
    from app.main import app

    rooms = [
        ("Living Room", "0xE23B2F", lambda p: p[0] > 140 and p[0] > p[1] + 40 and p[0] > p[2] + 40),
        ("Dining Area", "0x1D4ED8", lambda p: p[2] > 140 and p[2] > p[0] + 40),
        ("Modular Kitchen", "0x16A34A", lambda p: p[1] > 100 and p[1] > p[0] + 40 and p[1] > p[2] + 20),
        ("Master Bedroom", "0xF59E0B", lambda p: p[0] > 160 and p[1] > 100 and p[2] < 80),
    ]
    # Opening, Main moment, Detail, Story beat, Closing. The fourth photo repeats.
    assignment = [0, 1, 2, 3, 3]
    samples = [1.0, 8.0, 14.0, 20.0, 26.0]

    root = _IMAGE_REEL_ROOT
    client = TestClient(app)
    created = client.post("/api/projects", json={
        "idea": "a house shown room by room",
        "category": "Cinematic",
        "input_type": "Image",
        "duration": 30,
        "aspect_ratio": "9:16",
    })
    assert created.status_code == 201, created.text
    project = created.json()
    scenes = project["scenes"]
    assert [s["title"] for s in scenes] == [
        "Opening", "Main moment", "Detail", "Story beat", "Closing",
    ]
    assert [s["duration"] for s in scenes] == [6, 7, 6, 6, 5]
    assert sum(s["duration"] for s in scenes) == 30

    asset_ids = []
    for index, (name, color, _check) in enumerate(rooms):
        still = root / f"{name}.jpg"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=1280x720",
            "-frames:v", "1", str(still),
        ])
        uploaded = client.post(
            f"/api/projects/{project['id']}/uploads",
            params={"scene_id": scenes[index]["id"]},
            files={"file": (f"{name}.jpg", still.read_bytes(), "image/jpeg")},
        )
        assert uploaded.status_code == 201, uploaded.text
        asset_ids.append(next(
            s["asset_id"] for s in uploaded.json()["scenes"] if s["id"] == scenes[index]["id"]
        ))

    closing = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[4]['id']}/asset",
        json={"asset_id": asset_ids[3]},
    )
    assert closing.status_code == 200, closing.text
    mapped = [s["asset_id"] for s in closing.json()["scenes"]]
    assert mapped == [asset_ids[i] for i in assignment]
    assert len({a["id"] for a in closing.json()["assets"] if a["kind"] == "image"}) == 4

    # Switching Opening onto the kitchen photo must not move the other scenes.
    switched = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[0]['id']}/asset",
        json={"asset_id": asset_ids[2]},
    )
    assert switched.status_code == 200, switched.text
    after = [s["asset_id"] for s in switched.json()["scenes"]]
    assert after == [asset_ids[2], asset_ids[1], asset_ids[2], asset_ids[3], asset_ids[3]]
    restored = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[0]['id']}/asset",
        json={"asset_id": asset_ids[0]},
    )
    assert [s["asset_id"] for s in restored.json()["scenes"]] == [asset_ids[i] for i in assignment]

    started = client.post(f"/api/projects/{project['id']}/render", json={})
    assert started.status_code == 202, started.text
    jobs._execute(started.json()["job_id"], project["id"])

    done = client.get(f"/api/projects/{project['id']}").json()
    assert done["status"] == "ready", done.get("error")
    assert done["total_duration"] == 30
    video = root / done["video_url"].split("/media/", 1)[1]
    duration = ffmpeg.probe_duration(str(video))
    assert duration is not None and abs(duration - 30) < 0.05, duration
    info = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert info == "1080,1920,30/1", info

    clip_dir = root / "clips" / project["id"]
    for scene, room_index in zip(scenes, assignment):
        clips = list(clip_dir.glob(f"{scene['id']}_*.mp4"))
        assert len(clips) == 1, (scene["title"], clips)
        pixel = _rgb(clips[0], 0.4, "120:120:80:80")
        name, _color, check = rooms[room_index]
        print(f"  clip {scene['title']} {name} rgb={tuple(round(c) for c in pixel)}")
        assert check(pixel), (scene["title"], name, pixel)
        for color in ffmpeg.CARD_COLORS:
            card = _hex_rgb(color)
            distance = sum(abs(pixel[i] - card[i]) for i in range(3))
            assert distance > 80, (scene["title"], pixel, color)

    seen = []
    for at, room_index in zip(samples, assignment):
        pixel = _rgb(video, at, "120:120:80:80")
        name, _color, check = rooms[room_index]
        print(f"  frame {at:.0f}s {name} rgb={tuple(round(c) for c in pixel)}")
        assert check(pixel), (at, name, pixel)
        seen.append(pixel)
        for color in ffmpeg.CARD_COLORS:
            card = _hex_rgb(color)
            distance = sum(abs(pixel[i] - card[i]) for i in range(3))
            assert distance > 80, (at, pixel, color)
    # Neighbouring rooms are different photos. The last two samples are the same bedroom.
    for earlier, later in ((0, 1), (1, 2), (2, 3)):
        gap = sum(abs(seen[earlier][i] - seen[later][i]) for i in range(3))
        assert gap > 80, (samples[earlier], samples[later], gap)
    same = sum(abs(seen[3][i] - seen[4][i]) for i in range(3))
    assert same < 40, (seen[3], seen[4])
    print(
        f"  multi-image mp4 {video.name} {video.stat().st_size} bytes"
        f" {duration:.3f}s {info}"
    )


def test_uploaded_image_is_in_the_reel_and_cards_remain_without_one():
    """Upload path: one scene with a still, one without, exact duration.

    The still must survive project -> scene reference -> mock generator ->
    ffmpeg. A scene with no still still gets a title card.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped image reel: ffmpeg not on PATH)")
        return

    root = _IMAGE_REEL_ROOT
    try:
        # Imported after the env is set: config reads it once, at import.
        from fastapi.testclient import TestClient

        from app import jobs
        from app.main import app

        client = TestClient(app)
        project = client.post("/api/projects", json={
            "idea": "a modern living room",
            "category": "Cinematic",
            "duration": 6,
            "aspect_ratio": "9:16",
        })
        assert project.status_code == 201, project.text
        body = project.json()
        scenes = body["scenes"]
        assert sum(s["duration"] for s in scenes) == 6, scenes
        assert len(scenes) >= 2

        still = root / "room.png"
        # Wide split so a portrait Ken Burns both shows the photo and moves.
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=640x720",
            "-f", "lavfi", "-i", "color=c=0x1F8A4C:s=640x720",
            "-filter_complex", "hstack=inputs=2",
            "-frames:v", "1", str(still),
        ])
        uploaded = client.post(
            f"/api/projects/{body['id']}/uploads",
            params={"scene_id": scenes[0]["id"]},
            files={"file": ("room.png", still.read_bytes(), "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        linked = {s["id"]: s for s in uploaded.json()["scenes"]}
        assert linked[scenes[0]["id"]]["asset_url"], "upload did not attach to the scene"
        assert not linked[scenes[1]["id"]]["asset_url"]

        captioned = client.patch(
            f"/api/projects/{body['id']}/scenes/{scenes[0]['id']}",
            json={"caption": "Warm light"},
        )
        assert captioned.status_code == 200, captioned.text

        tone = root / "tone.wav"
        _media(tone, 2, audio=True)
        music = client.post(
            f"/api/projects/{body['id']}/uploads",
            files={"file": ("tone.wav", tone.read_bytes(), "audio/wav")},
        )
        assert music.status_code == 201, music.text

        started = client.post(f"/api/projects/{body['id']}/render", json={})
        assert started.status_code == 202, started.text
        jobs._execute(started.json()["job_id"], body["id"])

        finished = client.get(f"/api/projects/{body['id']}")
        assert finished.status_code == 200, finished.text
        done = finished.json()
        assert done["status"] == "ready", done.get("error")
        video_key = done["video_url"].split("/media/", 1)[1]
        video = root / video_key
        assert video.exists(), video

        duration = ffmpeg.probe_duration(str(video))
        assert duration is not None and abs(duration - 6) < 0.05, duration
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert info == "1080,1920,30/1", info

        # Top-left of the photo scene is the red half, not an indigo card.
        photo = _rgb(video, 0.4, "80:80:40:80")
        assert photo[0] > 140 and photo[0] > photo[2] + 40, photo
        for color in ffmpeg.CARD_COLORS:
            card = _hex_rgb(color)
            distance = sum(abs(photo[i] - card[i]) for i in range(3))
            assert distance > 80, (photo, color, distance)
        # The move is visible: a static slideshow would match frame to frame.
        assert _frame_delta(video, 0.25, scenes[0]["duration"] - 0.3) > 4

        # The next scene has no still, so it stays a title card.
        card_at = scenes[0]["duration"] + 0.4
        painted = _rgb(video, card_at, "80:80:40:80")
        expect = _hex_rgb(ffmpeg.CARD_COLORS[1])
        gap = sum(abs(painted[i] - expect[i]) for i in range(3))
        assert gap < 45, (painted, expect, gap)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def demo():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")

    print("\nsample 30s Product storyboard:")
    for s in storyboard.plan_scenes("a handmade leather wallet", "Product", 30):
        print(f"  {s['duration']:>2}s  {s['title']:<16} {s['prompt'][:58]}")


if __name__ == "__main__":
    demo()
