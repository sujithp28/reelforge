"""Self-check for the parts with real logic: duration maths and ffmpeg command
building. Plain asserts, no test framework.

    python test_reelforge.py
"""
import json
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

from app import config, ffmpeg, storyboard, typography


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
            # The default provider is the template, so source type and image names
            # do not change the storyboard.
            same = storyboard.plan(
                "a red bicycle", category, duration, "Image + text", ["room.jpg"],
            )
            assert same == scenes, (category, duration)
            assert storyboard.plan(
                "a red bicycle", category, duration, "Idea", [],
            ) == scenes


def test_plan_scenes_keeps_opening_and_closing_beats():
    short = storyboard.plan_scenes("x", "Cinematic", 12)
    sheet = storyboard.BEAT_SHEETS["Cinematic"]
    assert short[0]["title"] == sheet[0][0]
    assert short[-1]["title"] == sheet[-1][0]


def test_unknown_category_falls_back():
    scenes = storyboard.plan_scenes("x", "Definitely Not A Category", 30)
    assert sum(s["duration"] for s in scenes) == 30
    assert scenes[0]["title"] == storyboard.DEFAULT_SHEET[0][0]


def _model_scene(position, duration, asset_id=None, title="Room", prompt="Wide shot", caption=None):
    scene = {
        "position": position,
        "title": title,
        "prompt": prompt,
        "duration": duration,
        "caption": caption,
    }
    if asset_id is not None:
        scene["asset_id"] = asset_id
    return scene


def _model_brief(duration=6):
    return storyboard.StoryboardBrief(
        idea="a red bicycle",
        category="Product",
        duration=duration,
        input_type="Image",
        images=(
            storyboard.StoryboardImage(
                "asset_living", "living.jpg", "/media/uploads/p/asset_living.jpg", 0,
            ),
            storyboard.StoryboardImage(
                "asset_kitchen", "kitchen.jpg", "/media/uploads/p/asset_kitchen.jpg", 1,
            ),
        ),
    )


def _valid_model_payload():
    return {
        "scenes": [
            _model_scene(0, 2, "asset_living", title="Living room", caption="Welcome"),
            _model_scene(1, 2, title="Detail", caption=None),
            _model_scene(2, 2, "asset_kitchen", title="Kitchen"),
        ]
    }


def _expect_invalid(raw, brief=None):
    try:
        storyboard.validate_model_output(raw, brief or _model_brief())
    except ValueError:
        return
    raise AssertionError(f"expected invalid model output to be rejected: {raw}")


def test_storyboard_provider_defaults_to_template():
    assert "REELFORGE_STORYBOARD_PROVIDER" not in os.environ
    assert config.STORYBOARD_PROVIDER == "template"
    assert storyboard._provider_name("template") == "template"
    assert storyboard._provider_name("model") == "model"
    assert storyboard._provider_name("") == "template"
    assert storyboard._provider_name("openai") == "template"

    class Called:
        def plan(self, brief):
            raise AssertionError("the template provider must not call the model")

    previous = config.STORYBOARD_PROVIDER
    try:
        config.STORYBOARD_PROVIDER = "template"
        scenes = storyboard.plan("a red bicycle", "Product", 30, "Image", ["room.jpg"])
        assert scenes == storyboard.plan_scenes("a red bicycle", "Product", 30)
        planned = storyboard.plan_storyboard(
            "a red bicycle", "Product", 30, "Image", ["room.jpg"], model=Called(),
        )
        assert planned.scenes == scenes
        assert planned.assignments == {}
        config.STORYBOARD_PROVIDER = "not-a-provider"
        assert storyboard.plan(
            "a red bicycle", "Product", 30, "Idea", [],
        ) == scenes
    finally:
        config.STORYBOARD_PROVIDER = previous


def test_model_planner_interface_exists():
    brief = _model_brief()
    planner = storyboard.UnconnectedModelPlanner()
    assert callable(planner.plan)
    try:
        planner.plan(brief)
    except storyboard.StoryboardModelError:
        return
    raise AssertionError("the unconnected model planner must not invent a storyboard")


def test_model_output_validation():
    brief = _model_brief()
    planned = storyboard.validate_model_output(_valid_model_payload(), brief)
    assert [scene["position"] for scene in planned.scenes] == [0, 1, 2]
    assert planned.scenes[0]["title"] == "Living room"
    assert planned.scenes[0]["prompt"] == "Wide shot"
    assert planned.scenes[0]["caption"] == "Welcome"
    assert planned.scenes[1]["caption"] is None
    assert "asset_id" not in planned.scenes[0]
    assert planned.assignments == {0: "asset_living", 2: "asset_kitchen"}
    assert sum(scene["duration"] for scene in planned.scenes) == brief.duration

    _expect_invalid({"scenes": []})
    _expect_invalid({"scenes": [_model_scene(0, 6)]})
    short = _valid_model_payload()
    short["scenes"][2]["duration"] = 1
    _expect_invalid(short)
    zero = _valid_model_payload()
    zero["scenes"][0]["duration"] = 0
    _expect_invalid(zero)
    gap = _valid_model_payload()
    gap["scenes"][1]["position"] = 2
    _expect_invalid(gap)
    untitled = _valid_model_payload()
    untitled["scenes"][0]["title"] = "  "
    _expect_invalid(untitled)
    unprompted = _valid_model_payload()
    unprompted["scenes"][0]["prompt"] = ""
    _expect_invalid(unprompted)
    unknown = _valid_model_payload()
    unknown["scenes"][0]["asset_id"] = "asset_missing"
    _expect_invalid(unknown)


def test_invalid_model_output_uses_the_template():
    template = storyboard.plan_scenes("a red bicycle", "Product", 30)

    class Invalid:
        def plan(self, brief):
            return {"scenes": []}

    class TimedOut:
        def plan(self, brief):
            raise TimeoutError("timed out")

    class Good:
        def plan(self, brief):
            return _valid_model_payload()

    previous = config.STORYBOARD_PROVIDER
    config.STORYBOARD_PROVIDER = "model"
    try:
        invalid = storyboard.plan_storyboard(
            "a red bicycle", "Product", 30, "Image", ["room.jpg"], model=Invalid(),
        )
        assert invalid.scenes == template
        assert invalid.assignments == {}
        assert storyboard.plan(
            "a red bicycle", "Product", 30, "Image", ["room.jpg"],
        ) == template
        timed_out = storyboard.plan_storyboard(
            "a red bicycle", "Product", 30, "Idea", [], model=TimedOut(),
        )
        assert timed_out.scenes == template
        images = list(_model_brief().images)
        accepted = storyboard.plan_storyboard(
            "a red bicycle", "Product", 6, "Image", images=images, model=Good(),
        )
        assert accepted.scenes[0]["title"] == "Living room"
        assert accepted.assignments[0] == "asset_living"
        assert sum(scene["duration"] for scene in accepted.scenes) == 6
    finally:
        config.STORYBOARD_PROVIDER = previous


_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f9d0000000049454e44ae42"
    "6082"
)


def test_stored_assets_become_storyboard_images():
    assets = [
        {
            "id": "asset_a", "kind": "image", "filename": "living.jpg",
            "storage_key": "uploads/p/asset_a.jpg",
        },
        {
            "id": "asset_audio", "kind": "audio", "filename": "song.mp3",
            "storage_key": "uploads/p/song.mp3",
        },
        {
            "id": "asset_b", "kind": "image", "filename": "kitchen.jpg",
            "storage_key": "uploads/p/asset_b.jpg",
        },
    ]
    seen = []

    def reference_for(key):
        seen.append(key)
        assert not isinstance(key, bytes)
        return f"/media/{key}"

    images = storyboard.images_from_stored_assets(assets, reference_for)
    assert [image.position for image in images] == [0, 1]
    assert [image.asset_id for image in images] == ["asset_a", "asset_b"]
    assert [image.filename for image in images] == ["living.jpg", "kitchen.jpg"]
    assert images[0].reference == "/media/uploads/p/asset_a.jpg"
    assert images[1].reference == "/media/uploads/p/asset_b.jpg"
    assert seen == ["uploads/p/asset_a.jpg", "uploads/p/asset_b.jpg"]


def _create_image_project(client, input_type="Image"):
    res = client.post("/api/projects", json={
        "idea": "a red bicycle",
        "category": "Product",
        "input_type": input_type,
        "duration": 30,
        "aspect_ratio": "9:16",
    })
    assert res.status_code == 201, res.text
    return res.json()


def test_template_upload_keeps_single_and_multi_image_assignment():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    single = _create_image_project(client)
    template_titles = [scene["title"] for scene in single["scenes"]]
    uploaded = client.post(
        f"/api/projects/{single['id']}/uploads",
        files={"file": ("only.png", _PNG_1PX, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert len(body["scenes"]) == 1
    assert body["scenes"][0]["title"] == template_titles[0]
    assert body["scenes"][0]["duration"] == 30
    assert body["scenes"][0]["asset_id"] == body["assets"][0]["id"]
    assert body["scenes"][0]["caption"] == "a red bicycle"
    assert sum(scene["duration"] for scene in body["scenes"]) == 30

    multi = _create_image_project(client, "Image + text")
    ordered = sorted(multi["scenes"], key=lambda scene: scene["position"])
    current = None
    for index, scene in enumerate(ordered[:4]):
        current = client.post(
            f"/api/projects/{multi['id']}/uploads",
            params={
                "scene_id": scene["id"],
                "replan": "true" if index == 3 else "false",
            },
            files={"file": (f"room-{index}.png", _PNG_1PX, "image/png")},
        )
        assert current.status_code == 201, current.text
        current = current.json()
    scenes = sorted(current["scenes"], key=lambda scene: scene["position"])
    images = [asset for asset in current["assets"] if asset["kind"] == "image"]
    assert len(scenes) == 4
    assert [scene["asset_id"] for scene in scenes] == [asset["id"] for asset in images]
    assert len({scene["asset_id"] for scene in scenes}) == 4
    assert sum(scene["duration"] for scene in scenes) == 30
    assert scenes[0]["caption"] == "a red bicycle"
    assert all(scene["caption"] is None for scene in scenes[1:])


def test_post_upload_plan_applies_assignments_from_stored_images():
    from fastapi.testclient import TestClient

    from app import db, repo, storage
    from app.main import app

    client = TestClient(app)
    created = _create_image_project(client)
    ordered = sorted(created["scenes"], key=lambda scene: scene["position"])
    scene_ids = [scene["id"] for scene in ordered]
    for index, scene in enumerate(ordered[:2]):
        res = client.post(
            f"/api/projects/{created['id']}/uploads",
            params={"scene_id": scene["id"], "replan": "false"},
            files={"file": (f"room-{index}.png", _PNG_1PX, "image/png")},
        )
        assert res.status_code == 201, res.text

    seen = []

    class Recorder:
        def plan(self, brief):
            seen.append(brief)
            base = storyboard.plan_scenes(brief.idea, brief.category, brief.duration)
            scenes = []
            for index, scene in enumerate(base):
                item = {
                    "position": scene["position"],
                    "title": f"Planned {index}",
                    "prompt": f"Use {brief.images[0].filename}",
                    "duration": scene["duration"],
                    "caption": "From the photos" if index == 0 else None,
                }
                if index == 2:
                    item["asset_id"] = brief.images[0].asset_id
                scenes.append(item)
            return {"scenes": scenes}

    previous = config.STORYBOARD_PROVIDER
    config.STORYBOARD_PROVIDER = "model"
    try:
        with db.connect() as conn:
            project = repo.get_project(conn, created["id"])
            storyboard.replan_uploaded_project(
                conn, project, storage.storage.url_for, model=Recorder(),
            )
            saved = repo.list_scenes(conn, created["id"])
            stored = [
                asset for asset in repo.list_assets(conn, created["id"])
                if asset["kind"] == "image"
            ]
    finally:
        config.STORYBOARD_PROVIDER = previous

    assert len(seen) == 1
    brief = seen[0]
    assert isinstance(brief, storyboard.StoryboardBrief)
    assert brief.idea == "a red bicycle"
    assert brief.category == "Product"
    assert brief.duration == 30
    assert brief.input_type == "Image"
    assert [image.asset_id for image in brief.images] == [asset["id"] for asset in stored]
    assert [image.filename for image in brief.images] == ["room-0.png", "room-1.png"]
    assert [image.position for image in brief.images] == [0, 1]
    assert all(image.reference.startswith("/media/uploads/") for image in brief.images)
    assert [scene["id"] for scene in saved] == scene_ids
    assert [scene["title"] for scene in saved] == [f"Planned {i}" for i in range(len(saved))]
    assert saved[0]["caption"] == "From the photos"
    assert saved[2]["asset_id"] == stored[0]["id"]
    assert saved[1]["asset_id"] == stored[1]["id"]
    assert sum(scene["duration"] for scene in saved) == 30


class _ReplanSpy:
    """Count post-upload planner calls and the images each call received."""

    def __init__(self):
        self.batches = []
        self._original = storyboard.replan_uploaded_project

    def __enter__(self):
        def spy(conn, project, reference_for, model=None):
            from app import repo
            self.batches.append(storyboard.images_from_stored_assets(
                repo.list_assets(conn, project["id"]), reference_for,
            ))
            return self._original(conn, project, reference_for, model)

        storyboard.replan_uploaded_project = spy
        return self

    def __exit__(self, *_args):
        storyboard.replan_uploaded_project = self._original
        return False


def test_one_image_upload_plans_once():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    created = _create_image_project(client)
    with _ReplanSpy() as spy:
        uploaded = client.post(
            f"/api/projects/{created['id']}/uploads",
            files={"file": ("only.png", _PNG_1PX, "image/png")},
        )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert len(spy.batches) == 1
    assert [image.filename for image in spy.batches[0]] == ["only.png"]
    assert len(body["scenes"]) == 1
    assert body["scenes"][0]["asset_id"] == body["assets"][0]["id"]
    assert body["scenes"][0]["duration"] == 30
    assert body["scenes"][0]["caption"] == "a red bicycle"
    assert sum(scene["duration"] for scene in body["scenes"]) == 30


def test_five_image_batch_plans_once():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    created = _create_image_project(client)
    ordered = sorted(created["scenes"], key=lambda scene: scene["position"])
    assert len(ordered) == 5
    body = None
    with _ReplanSpy() as spy:
        for index, scene in enumerate(ordered):
            res = client.post(
                f"/api/projects/{created['id']}/uploads",
                params={
                    "scene_id": scene["id"],
                    "replan": "true" if index == len(ordered) - 1 else "false",
                },
                files={"file": (f"room-{index}.png", _PNG_1PX, "image/png")},
            )
            assert res.status_code == 201, res.text
            body = res.json()
    assert len(spy.batches) == 1
    batch = spy.batches[0]
    assert [image.filename for image in batch] == [f"room-{index}.png" for index in range(5)], [
        image.filename for image in batch
    ]
    assert [image.position for image in batch] == [0, 1, 2, 3, 4]
    scenes = sorted(body["scenes"], key=lambda scene: scene["position"])
    assert len(scenes) == 5
    assert [scene["asset_id"] for scene in scenes] == [image.asset_id for image in batch]
    assert len({scene["asset_id"] for scene in scenes}) == 5
    assert sum(scene["duration"] for scene in scenes) == 30


def _upload_three_and_cover_the_rest(client, created):
    """Three photos on the first scenes, then the last photo covers the rest.

    The planner runs on the last cover step, after upload order is in place.
    """
    ordered = sorted(created["scenes"], key=lambda scene: scene["position"])
    assert len(ordered) == 5
    asset_ids = []
    body = None
    for index in range(3):
        res = client.post(
            f"/api/projects/{created['id']}/uploads",
            params={"scene_id": ordered[index]["id"], "replan": "false"},
            files={"file": (f"room-{index}.png", _PNG_1PX, "image/png")},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        asset_ids.append(next(
            scene["asset_id"] for scene in body["scenes"] if scene["id"] == ordered[index]["id"]
        ))
    for index in range(3, 5):
        res = client.put(
            f"/api/projects/{created['id']}/scenes/{ordered[index]['id']}/asset",
            params={"replan": "true" if index == 4 else "false"},
            json={"asset_id": asset_ids[2]},
        )
        assert res.status_code == 200, res.text
        body = res.json()
    return ordered, asset_ids, body


def _model_payload(brief, assignments):
    base = storyboard.plan_scenes(brief.idea, brief.category, brief.duration)
    scenes = []
    for scene in base:
        item = {
            "position": scene["position"],
            "title": f"Model {scene['position']}",
            "prompt": f"Model prompt {scene['position']}",
            "duration": scene["duration"],
            "caption": None,
        }
        asset_id = assignments.get(scene["position"])
        if asset_id is not None:
            item["asset_id"] = asset_id
        scenes.append(item)
    return {"scenes": scenes}


def test_template_batch_keeps_upload_order_assignments():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    created = _create_image_project(client)
    with _ReplanSpy() as spy:
        ordered, asset_ids, body = _upload_three_and_cover_the_rest(client, created)
    assert len(spy.batches) == 1
    assert [image.filename for image in spy.batches[0]] == ["room-0.png", "room-1.png", "room-2.png"]
    scenes = sorted(body["scenes"], key=lambda scene: scene["position"])
    assert [scene["asset_id"] for scene in scenes] == asset_ids
    assert len(scenes) == 3
    assert len({scene["asset_id"] for scene in scenes}) == 3
    assert sum(scene["duration"] for scene in scenes) == 30
    assert scenes[0]["caption"] == created["idea"]
    assert all(scene["caption"] is None for scene in scenes[1:])


def test_model_assignments_are_not_replaced_by_upload_order():
    from fastapi.testclient import TestClient

    from app.main import app

    class Permute:
        def plan(self, brief):
            ids = [image.asset_id for image in brief.images]
            return _model_payload(brief, {0: ids[2], 1: ids[0], 2: ids[1]})

    client = TestClient(app)
    created = _create_image_project(client)
    previous_provider = config.STORYBOARD_PROVIDER
    previous_planner = storyboard.UnconnectedModelPlanner
    config.STORYBOARD_PROVIDER = "model"
    storyboard.UnconnectedModelPlanner = Permute
    try:
        with _ReplanSpy() as spy:
            ordered, asset_ids, body = _upload_three_and_cover_the_rest(client, created)
        assert len(spy.batches) == 1
    finally:
        config.STORYBOARD_PROVIDER = previous_provider
        storyboard.UnconnectedModelPlanner = previous_planner

    scenes = sorted(body["scenes"], key=lambda scene: scene["position"])
    # The finished batch is one scene per uploaded image, in upload order.
    # A model permutation must not add extra scenes that reuse the last photo.
    assert [scene["asset_id"] for scene in scenes] == asset_ids
    assert len(scenes) == 3
    assert "Model 0" not in {scene["title"] for scene in scenes}
    assert sum(scene["duration"] for scene in scenes) == 30


def test_invalid_model_assignments_keep_upload_order():
    from fastapi.testclient import TestClient

    from app.main import app

    class UnknownAsset:
        def plan(self, brief):
            return _model_payload(brief, {0: "asset_missing"})

    class BadPosition:
        def plan(self, brief):
            asset = brief.images[0].asset_id if brief.images else "asset_missing"
            raw = _model_payload(brief, {0: asset})
            raw["scenes"][2]["position"] = 9
            return raw

    class TooFewScenes:
        def plan(self, brief):
            if not brief.images:
                raise ValueError("no images yet")
            scenes = []
            for index in range(3):
                item = {
                    "position": index,
                    "title": "Nope",
                    "prompt": "Nope",
                    "duration": 10,
                    "caption": None,
                }
                if index < len(brief.images):
                    item["asset_id"] = brief.images[index].asset_id
                scenes.append(item)
            return {"scenes": scenes}

    client = TestClient(app)
    created = _create_image_project(client)
    previous_provider = config.STORYBOARD_PROVIDER
    previous_planner = storyboard.UnconnectedModelPlanner
    config.STORYBOARD_PROVIDER = "model"
    try:
        for planner in (UnknownAsset, BadPosition, TooFewScenes):
            storyboard.UnconnectedModelPlanner = planner
            project = _create_image_project(client)
            _ordered, asset_ids, body = _upload_three_and_cover_the_rest(client, project)
            saved = sorted(body["scenes"], key=lambda scene: scene["position"])
            assert [scene["asset_id"] for scene in saved] == asset_ids, planner.__name__
            assert len(saved) == 3
            assert sum(scene["duration"] for scene in saved) == 30
            assert "Nope" not in {scene["title"] for scene in saved}
            assert "Model 0" not in {scene["title"] for scene in saved}
    finally:
        config.STORYBOARD_PROVIDER = previous_provider
        storyboard.UnconnectedModelPlanner = previous_planner


def test_audio_upload_does_not_plan():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    created = _create_image_project(client)
    with _ReplanSpy() as spy:
        import io
        import wave
        tone = io.BytesIO()
        with wave.open(tone, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 800)
        uploaded = client.post(
            f"/api/projects/{created['id']}/uploads",
            files={"file": ("song.wav", tone.getvalue(), "audio/wav")},
        )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert spy.batches == []
    assert all(scene["asset_id"] is None for scene in body["scenes"])
    assert [scene["title"] for scene in body["scenes"]] == [scene["title"] for scene in created["scenes"]]


def test_model_failure_after_upload_uses_the_template():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    previous = config.STORYBOARD_PROVIDER
    config.STORYBOARD_PROVIDER = "model"
    try:
        created = _create_image_project(client)
        template = storyboard.plan_scenes("a red bicycle", "Product", 30)
        with _ReplanSpy() as spy:
            uploaded = client.post(
                f"/api/projects/{created['id']}/uploads",
                files={"file": ("only.png", _PNG_1PX, "image/png")},
            )
        assert len(spy.batches) == 1
        assert [image.filename for image in spy.batches[0]] == ["only.png"]
    finally:
        config.STORYBOARD_PROVIDER = previous

    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert len(body["scenes"]) == 1
    assert body["scenes"][0]["title"] == template[0]["title"]
    assert body["scenes"][0]["prompt"] == template[0]["prompt"]
    assert body["scenes"][0]["duration"] == 30
    assert body["scenes"][0]["caption"] == "a red bicycle"
    assert body["scenes"][0]["asset_id"] == body["assets"][0]["id"]
    assert sum(scene["duration"] for scene in body["scenes"]) == 30


def test_reprompt_varies_between_attempts():
    a = storyboard.reprompt_scene("x", "Product", "In use", 0)
    b = storyboard.reprompt_scene("x", "Product", "In use", 1)
    assert a != b
    assert "In use" not in a  # it returns a prompt, not a title


def test_resolution_for():
    assert storyboard.resolution_for("9:16") == (1080, 1920)
    assert storyboard.resolution_for("16:9") == (1920, 1080)
    assert storyboard.resolution_for("1:1") == (1080, 1080)
    assert storyboard.resolution_for("4:5") == (1080, 1350)
    assert storyboard.resolution_for("nonsense") == (1080, 1920)


def test_export_presets_match_the_publish_formats():
    from app import export_presets

    expected = {
        "instagram_reel": ("9:16", 1080, 1920),
        "youtube_short": ("9:16", 1080, 1920),
        "instagram_feed": ("4:5", 1080, 1350),
        "youtube_landscape": ("16:9", 1920, 1080),
    }
    assert set(export_presets.PRESETS) == set(expected)
    for preset_id, (ratio, width, height) in expected.items():
        preset = export_presets.PRESETS[preset_id]
        assert preset.aspect_ratio == ratio
        assert (preset.width, preset.height) == (width, height)
        assert preset.fps == 30
        assert preset.video_codec == "libx264"
        assert preset.pix_fmt == "yuv420p"
        assert "1080" not in preset.label and "H.264" not in preset.label
    # A project saved before presets keeps the frame it already used.
    legacy = export_presets.resolve(None, "1:1")
    assert (legacy.width, legacy.height) == (1080, 1080)
    vertical = export_presets.resolve(None, "9:16")
    assert (vertical.width, vertical.height) == (1080, 1920)
    named = export_presets.resolve("instagram_feed", "9:16")
    assert (named.width, named.height) == (1080, 1350)
    filters = Path(ffmpeg.__file__).read_text(encoding="utf-8").lower()
    assert "instagram" not in filters and "youtube" not in filters


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
        image="in.jpg", out="out.mp4", seconds=7, width=1080, height=1920,
        caption="Hi", text_title="Hi",
    )
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd and "in.jpg" in cmd and cmd[-1] == "out.mp4"
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1080:1920" in vf and "drawtext" in vf and "crop=w=" in vf
    # zoompan holds the first step on a still, so the move is an animated crop.
    assert "zoompan" not in vf, vf
    assert "force_original_aspect_ratio=increase" in vf
    assert "eq=contrast=" in vf and "vignette=" in vf
    assert "cos(PI*" in vf
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


def test_every_motion_path_moves_on_a_two_axis_still():
    """Every Ken Burns path has to move when the still varies on both axes.

    A left/right split hides the vertical drift: the crop stays centered on
    that edge, so the fifth path looks frozen. This fixture is a four-color
    grid, so a horizontal pan and a vertical drift both change the picture.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped two-axis motion: ffmpeg not on PATH)")
        return

    root = Path(tempfile.mkdtemp(prefix="reelforge-motion-"))
    try:
        still = root / "grid.png"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=960x540",
            "-f", "lavfi", "-i", "color=c=0x1D4ED8:s=960x540",
            "-f", "lavfi", "-i", "color=c=0x16A34A:s=960x540",
            "-f", "lavfi", "-i", "color=c=0xF59E0B:s=960x540",
            "-filter_complex",
            "[0:v][1:v]hstack=inputs=2[top];"
            "[2:v][3:v]hstack=inputs=2[bot];"
            "[top][bot]vstack=inputs=2",
            "-frames:v", "1", str(still),
        ])
        filters = []
        for index in range(len(ffmpeg.MOTION_PATHS)):
            clip = root / f"path_{index}.mp4"
            cmd = ffmpeg.build_still_clip_cmd(
                image=str(still), out=str(clip), seconds=3,
                width=540, height=960, caption=None, index=index,
            )
            filters.append(cmd[cmd.index("-vf") + 1])
            ffmpeg.run(cmd)
            delta = _frame_delta(clip, 0.2, 2.6)
            # The move is a few percent of the frame. A frozen crop is ~0.
            assert delta > 1.5, (ffmpeg.motion_kind(index, "wide"), delta)
        assert len(set(filters)) == len(ffmpeg.MOTION_PATHS)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_motion_and_transitions_stay_deterministic():
    assert [ffmpeg.motion_kind(i, "unknown") for i in range(9)] == list(ffmpeg.MOTION_PATHS)
    landscape = [ffmpeg.motion_kind(i, "landscape") for i in range(9)]
    portrait = [ffmpeg.motion_kind(i, "portrait") for i in range(9)]
    square = [ffmpeg.motion_kind(i, "square") for i in range(9)]
    for order in (landscape, portrait, square):
        assert sorted(order) == sorted(ffmpeg.MOTION_PATHS), order
    assert landscape[0] in ("drift_lr", "drift_rl", "push_drift", "pull_drift")
    assert portrait[0] in ("drift_ud", "drift_du", "diagonal")
    assert square[0] in ("diagonal", "push_in", "pull_out")
    assert landscape != portrait
    wide = [ffmpeg.motion_kind(i, "wide") for i in range(9)]
    tall = [ffmpeg.motion_kind(i, "tall") for i in range(9)]
    assert sorted(wide) == sorted(ffmpeg.MOTION_PATHS)
    assert sorted(tall) == sorted(ffmpeg.MOTION_PATHS)
    assert wide[0] in ("push_in", "pull_out")
    assert tall[0] in ("drift_ud", "drift_du", "push_in", "pull_out")
    for order in (landscape, portrait, square, wide, tall, list(ffmpeg.MOTION_PATHS)):
        families = [ffmpeg.motion_family(kind) for kind in order]
        assert all(a != b for a, b in zip(families, families[1:])), families
    assert ffmpeg.orientation_from_size(1920, 1080) == "wide"
    assert ffmpeg.orientation_from_size(1600, 1200) == "landscape"
    assert ffmpeg.orientation_from_size(1080, 1080) == "square"
    assert ffmpeg.orientation_from_size(1080, 1350) == "portrait"
    assert ffmpeg.orientation_from_size(1080, 1920) == "tall"

    kwargs = dict(image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920, caption=None)
    first = ffmpeg.build_still_clip_cmd(**kwargs, index=0)
    # The window eases around the center of the photo, not across its spare edges.
    move = first[first.index("-vf") + 1]
    assert ")/2+(" in move
    assert "0.880" not in move and "1.060" not in move
    assert "eq=contrast=1.02:saturation=1.00" in move
    assert first == ffmpeg.build_still_clip_cmd(**kwargs, index=0)
    other = ffmpeg.build_still_clip_cmd(**kwargs, index=1)
    assert first[first.index("-vf") + 1] != other[other.index("-vf") + 1]

    assert [ffmpeg.transition_for(i) for i in range(8)] == ["wipeleft"] * 8
    assert ffmpeg.transition_for(1) == ffmpeg.transition_for(1)

    opening = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920,
        text_title="Modern luxury interior showcase", index=0,
    )
    quiet = ffmpeg.build_still_clip_cmd(**kwargs, index=1)
    opening_vf = opening[opening.index("-vf") + 1]
    assert "drawtext" in opening_vf and "Modern luxury interior showcase" in opening_vf
    assert "drawtext" not in quiet[quiet.index("-vf") + 1]
    assert opening == ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920,
        text_title="Modern luxury interior showcase", index=0,
    )
    # A quiet lower third: the supplied words only, no plate, gone before the join.
    assert "box=1" not in opening_vf
    assert "shadowcolor=black@0.85" in opening_vf and "borderw=1" in opening_vf
    opening_block, _ = typography.compose(
        "Modern luxury interior showcase", None,
        ffmpeg.find_title_font(), ffmpeg.find_subtitle_font(), 1080, 1920,
    )
    assert opening_block is not None
    assert f"fontsize={opening_block.fontsize}" in opening_vf
    out_end = 6 - (ffmpeg.CROSSFADE_SECONDS / 2)
    out_start = out_end - ffmpeg.TITLE_FADE_SECONDS
    assert f"{ffmpeg.TITLE_FADE_SECONDS:.3f}" in opening_vf
    assert f"{out_start:.3f}" in opening_vf
    assert f"{out_end:.3f}" in opening_vf
    assert out_end < 6

    # Preview and download still receive a faststart remux, not a second encode.
    finalize = ffmpeg.build_finalize_cmd("in.mp4", "out.mp4")
    assert finalize[finalize.index("-c") + 1] == "copy"
    assert "+faststart" in finalize


def test_reel_duration_frame_and_cover_crop():
    """Transitions must not stretch the reel, the frame, or the photograph."""
    if not ffmpeg.ffmpeg_available():
        print("  (skipped cinematic duration: ffmpeg not on PATH)")
        return

    from app import render
    from app.providers import MockGenerator

    root = Path(tempfile.mkdtemp(prefix="reelforge-cine-"))
    try:
        def paint(name: str, spec: str) -> Path:
            path = root / name
            ffmpeg.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", spec,
                "-frames:v", "1", str(path),
            ])
            return path

        red = paint("red.jpg", "color=c=0xE23B2F:s=1280x720")
        blue = paint("blue.jpg", "color=c=0x1D4ED8:s=1280x720")
        for total, parts in ((5, (2, 3)), (10, (4, 6)), (15, (7, 8)), (30, (14, 16))):
            scenes = []
            images = {}
            for index, seconds in enumerate(parts):
                scene_id = f"d{total}_{index}"
                scenes.append({
                    "id": scene_id, "title": f"Room {index}", "prompt": "p",
                    "duration": seconds,
                    "caption": "Modern luxury interior showcase" if index == 0 else None,
                })
                images[scene_id] = red if index == 0 else blue
            out = root / f"{total}.mp4"
            render.render_reel(
                scenes=scenes, images=images, aspect_ratio="9:16",
                work_dir=root / f"work-{total}", out_path=out, generator=MockGenerator(),
            )
            duration = ffmpeg.probe_duration(str(out))
            assert duration is not None and abs(duration - total) < 0.05, (total, duration)
            if total != 5:
                continue
            info = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate",
                 "-of", "csv=p=0", str(out)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert info == "1080,1920,30/1", info
            # Inset corners: a black bar would be near zero, a mild vignette would not.
            for crop in ("32:32:12:12", "32:32:1036:12", "32:32:12:1876", "32:32:1036:1876"):
                pixel = _rgb(out, 0.8, crop)
                assert min(pixel) > 20 and pixel[0] > pixel[2], (crop, pixel)
            center = _rgb(out, 0.8, "80:80:500:920")
            assert center[0] > 150 and center[0] > center[1] + 40, center

        edged = root / "edged.jpg"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x2244EE:s=80x400",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=1440x400",
            "-f", "lavfi", "-i", "color=c=0x2244EE:s=80x400",
            "-filter_complex", "hstack=inputs=3",
            "-frames:v", "1", str(edged),
        ])
        covered = root / "covered.mp4"
        render.render_reel(
            scenes=[{
                "id": "edge", "title": "Edge", "prompt": "p",
                "duration": 2, "caption": None,
            }],
            images={"edge": edged}, aspect_ratio="9:16",
            work_dir=root / "work-edge", out_path=covered, generator=MockGenerator(),
        )
        # The blue strips sit on the source edges. A cover crop keeps the red
        # centre; a stretch or a fit-with-bars would show blue or black.
        for crop in ("40:40:16:940", "40:40:1024:940", "40:40:520:16", "40:40:520:1864"):
            pixel = _rgb(covered, 0.6, crop)
            assert pixel[0] > 140 and pixel[0] > pixel[2] + 40, (crop, pixel)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_scene_design_text_is_optional_and_leaves_the_upload_alone():
    """Design text is per scene. The project idea is not burned onto the photo."""
    from app import render

    titled = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920,
        caption="Modern luxury interior showcase", text_title="Modern Luxury Living",
    )
    both = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920,
        text_title="Elegant Dining", text_subtitle="Crafted for memorable moments",
    )
    plain = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920,
        caption="Modern luxury interior showcase",
    )
    later = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920, index=1,
    )
    titled_vf = titled[titled.index("-vf") + 1]
    both_vf = both[both.index("-vf") + 1]
    assert "Modern Luxury Living" in titled_vf
    assert "Modern luxury interior showcase" not in titled_vf
    assert "box=1" not in titled_vf and "box=1" not in both_vf
    assert "Elegant Dining" in both_vf and "Crafted for memorable moments" in both_vf
    title_block, subtitle_block = typography.compose(
        "Elegant Dining", "Crafted for memorable moments",
        ffmpeg.find_title_font(), ffmpeg.find_subtitle_font(), 1080, 1920,
    )
    assert title_block is not None and subtitle_block is not None
    assert title_block.fontsize > subtitle_block.fontsize
    assert f"fontsize={title_block.fontsize}" in both_vf
    assert f"fontsize={subtitle_block.fontsize}" in both_vf
    assert "fontcolor=white:" in both_vf and "fontcolor=white@0.86" in both_vf
    assert "h*0.900-text_h" in both_vf
    assert "drawtext" not in plain[plain.index("-vf") + 1]
    assert "drawtext" not in later[later.index("-vf") + 1]
    assert render.visible_overlay({
        "text_title": "Hidden", "show_title": 0,
        "text_subtitle": "Also hidden", "show_subtitle": 0,
    }) == (None, None)
    assert render.visible_overlay({
        "text_title": "Shown", "show_title": 1,
        "text_subtitle": "Line", "show_subtitle": 1,
    }) == ("Shown", "Line")

    if not ffmpeg.ffmpeg_available():
        print("  (skipped design-text render: ffmpeg not on PATH)")
        return
    from app.providers import MockGenerator

    root = Path(tempfile.mkdtemp(prefix="reelforge-text-"))
    try:
        still = root / "room.png"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=640x360",
            "-frames:v", "1", str(still),
        ])
        original = still.read_bytes()
        scenes = [
            {
                "id": "s0", "title": "Approach", "prompt": "p", "duration": 2,
                "caption": "Modern luxury interior showcase",
                "text_title": "Modern Luxury Living", "show_title": 1,
            },
            {
                "id": "s1", "title": "Entry", "prompt": "p", "duration": 2,
                "caption": None,
            },
        ]
        first = render.build_scene_spec(scenes[0], 0, 1080, 1920, still, "standard")
        second = render.build_scene_spec(scenes[1], 1, 1080, 1920, still, "standard")
        assert first.text_title == "Modern Luxury Living" and first.text_subtitle is None
        assert second.text_title is None and second.text_subtitle is None
        out = root / "reel.mp4"
        render.render_reel(
            scenes=scenes, images={"s0": still, "s1": still}, aspect_ratio="9:16",
            work_dir=root / "work", out_path=out, generator=MockGenerator(),
        )
        assert still.read_bytes() == original
        duration = ffmpeg.probe_duration(str(out))
        assert duration is not None and abs(duration - 4) < 0.05, duration
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_plan_scenes_puts_the_idea_on_the_first_caption_only():
    scenes = storyboard.plan_scenes("a red bicycle", "Product", 30)
    assert scenes[0]["caption"] == "a red bicycle"
    assert all(scene["caption"] is None for scene in scenes[1:])


def test_image_timeline_is_one_scene_per_photo():
    for count in (1, 3, 5, 8):
        scenes = storyboard.scenes_for_image_count("harbour light", "Cinematic", 30, count)
        assert len(scenes) == count
        assert sum(scene["duration"] for scene in scenes) == 30
        assert min(scene["duration"] for scene in scenes) >= 1
        assert scenes[0]["caption"] == "harbour light"
        assert all(scene["caption"] is None for scene in scenes[1:])
    one = storyboard.scenes_for_image_count("harbour light", "Cinematic", 30, 1)
    assert one[0]["duration"] == 30


def test_same_asset_boundary_is_a_cut_and_different_images_still_fade():
    from app.render import assembly_joins, assembly_stages

    assert assembly_joins(["a", "a"]) == ["cut"]
    assert assembly_joins(["a", "b"]) == ["fade"]
    assert assembly_joins(["a", "b", "c"]) == ["fade", "fade"]
    assert assembly_joins(["a", "a", "b", "b"]) == ["cut", "fade", "cut"]
    # Two title cards are not the same photo, so they keep the fade.
    assert assembly_joins([None, None]) == ["fade"]
    assert assembly_stages(["a", "a"]) == ["concat"]
    assert assembly_stages(["a", "b", "c"]) == ["crossfade"]
    assert assembly_stages(["a", "a", "b"]) == ["concat", "crossfade"]


def test_crossfade_command_wipes_without_a_dip_to_black():
    cmd = ffmpeg.build_crossfade_cmd(["a.mp4", "b.mp4", "c.mp4"], [6, 7, 5], "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("xfade=") == 2
    assert graph.count("transition=wipeleft:") == 2
    assert "transition=fade:" not in graph
    assert "smoothleft" not in graph
    assert "zoomin" not in graph
    assert "fadeblack" not in graph
    longer = ffmpeg.build_crossfade_cmd(
        ["a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4"], [4, 4, 4, 4, 4], "out.mp4",
    )
    longer_graph = longer[longer.index("-filter_complex") + 1]
    assert longer_graph.count("transition=wipeleft:") == 4
    assert "smoothleft" not in longer_graph
    assert "transition=fade:" not in longer_graph
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
            picture = ffmpeg.probe_duration(str(video))
            assert picture is not None and duration is not None
            assert abs(duration - picture) < (1 / 30) + 0.02, (name, duration, picture)
            assert duration + 0.04 >= picture
            streams = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", str(out)],
                capture_output=True, text=True, check=True,
            ).stdout
            if music is None:
                assert "audio" not in streams
            else:
                assert "audio" in streams
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
    """A wipe between stills must not shorten the reel or mix the photos."""
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
        # Mid-wipe: the outgoing picture still owns the left, the incoming the right.
        # A dissolve would mix both colours across the whole frame.
        outgoing = _rgb(out, 2.0, "40:40:40:900")
        incoming = _rgb(out, 2.0, "40:40:1000:900")
        assert outgoing[0] > 140 and outgoing[0] > outgoing[2] + 40, outgoing
        assert incoming[2] > 140 and incoming[2] > incoming[0] + 40, incoming

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


def _has_light_text(video: Path, at: float, width: int, height: int) -> bool:
    """True when the lower-third band contains a near-white pixel."""
    y = int(height * 0.82)
    band = max(24, int(height * 0.10))
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(video),
            "-frames:v", "1", "-vf", f"crop={width - 80}:{band}:40:{y}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True, check=True,
    )
    data = proc.stdout
    for index in range(0, len(data) - 2, 3):
        if data[index] > 245 and data[index + 1] > 245 and data[index + 2] > 245:
            return True
    return False


def test_export_presets_render_the_publish_frame():
    """Each publish format is a real MP4 at that frame, with the photo still covering it."""
    if not ffmpeg.ffmpeg_available():
        print("  (skipped export presets: ffmpeg not on PATH)")
        return

    from app import export_presets, render
    from app.providers import MockGenerator

    root = Path(tempfile.mkdtemp(prefix="reelforge-preset-"))
    try:
        wide = root / "wide.jpg"
        tall = root / "tall.jpg"
        grid = root / "grid.png"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x2244EE:s=80x400",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=1440x400",
            "-f", "lavfi", "-i", "color=c=0x2244EE:s=80x400",
            "-filter_complex", "hstack=inputs=3",
            "-frames:v", "1", str(wide),
        ])
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0x2244EE:s=400x80",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=400x1440",
            "-f", "lavfi", "-i", "color=c=0x2244EE:s=400x80",
            "-filter_complex", "vstack=inputs=3",
            "-frames:v", "1", str(tall),
        ])
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=960x540",
            "-f", "lavfi", "-i", "color=c=0x1D4ED8:s=960x540",
            "-f", "lavfi", "-i", "color=c=0x16A34A:s=960x540",
            "-f", "lavfi", "-i", "color=c=0xF59E0B:s=960x540",
            "-filter_complex",
            "[0:v][1:v]hstack=inputs=2[top];"
            "[2:v][3:v]hstack=inputs=2[bot];"
            "[top][bot]vstack=inputs=2",
            "-frames:v", "1", str(grid),
        ])

        def probe(path: Path) -> dict:
            raw = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,r_frame_rate,codec_name,pix_fmt",
                 "-of", "json", str(path)],
                capture_output=True, text=True, check=True,
            ).stdout
            return json.loads(raw)["streams"][0]

        def covered(path: Path, at: float, width: int, height: int) -> None:
            inset = 20
            crops = [
                f"28:28:{inset}:{inset}",
                f"28:28:{width - inset - 28}:{inset}",
                f"28:28:{inset}:{height - inset - 28}",
                f"28:28:{width - inset - 28}:{height - inset - 28}",
                f"40:40:{(width - 40) // 2}:{(height - 40) // 2}",
            ]
            for crop in crops:
                pixel = _rgb(path, at, crop)
                assert min(pixel) > 20, (path.name, crop, pixel)
                assert pixel[0] > pixel[2] + 40, (path.name, crop, pixel)

        for preset_id, preset in export_presets.PRESETS.items():
            still = tall if preset.aspect_ratio == "16:9" else wide
            titled = preset_id == "instagram_reel"
            scenes = [{
                "id": "a", "title": "Approach", "prompt": "p", "duration": 2,
                "show_title": 1 if titled else 0,
                "show_subtitle": 1 if titled else 0,
                "text_title": "Modern Luxury Living",
                "text_subtitle": "Designed for modern life",
            }]
            images = {"a": still}
            if titled:
                scenes.append({
                    "id": "b", "title": "Entry", "prompt": "p", "duration": 2,
                    "show_title": 0, "text_title": "Should stay off",
                })
                images["b"] = still
            out = root / f"{preset_id}.mp4"
            render.render_reel(
                scenes=scenes, images=images, aspect_ratio="1:1",
                work_dir=root / f"work-{preset_id}", out_path=out,
                generator=MockGenerator(), preset=preset,
            )
            expected = 4 if titled else 2
            duration = ffmpeg.probe_duration(str(out))
            assert duration is not None and abs(duration - expected) < 0.05, (preset_id, duration)
            info = probe(out)
            assert info["codec_name"] == "h264", (preset_id, info)
            assert info["pix_fmt"] == "yuv420p", (preset_id, info)
            assert int(info["width"]) == preset.width and int(info["height"]) == preset.height
            assert info["r_frame_rate"] == "30/1", (preset_id, info)
            covered(out, 0.5, preset.width, preset.height)
            if titled:
                assert _has_light_text(out, 1.0, preset.width, preset.height)
                assert not _has_light_text(out, 3.0, preset.width, preset.height)
                covered(out, 3.0, preset.width, preset.height)

        motion = root / "motion.mp4"
        short = export_presets.PRESETS["youtube_short"]
        render.render_reel(
            scenes=[{"id": "a", "title": "Approach", "prompt": "p", "duration": 2}],
            images={"a": grid}, aspect_ratio="9:16",
            work_dir=root / "work-motion", out_path=motion,
            generator=MockGenerator(), preset=short,
        )
        # A few percent of travel still changes the picture. A frozen frame is ~0.
        assert _frame_delta(motion, 0.2, 1.6) > 1.5

        legacy = root / "legacy.mp4"
        render.render_reel(
            scenes=[{"id": "a", "title": "Approach", "prompt": "p", "duration": 2}],
            images={"a": wide}, aspect_ratio="9:16",
            work_dir=root / "work-legacy", out_path=legacy,
            generator=MockGenerator(),
        )
        assert probe(legacy)["width"] == 1080 and probe(legacy)["height"] == 1920
        assert probe(legacy)["codec_name"] == "h264" and probe(legacy)["pix_fmt"] == "yuv420p"
        square = root / "square.mp4"
        render.render_reel(
            scenes=[{"id": "a", "title": "Approach", "prompt": "p", "duration": 2}],
            images={"a": wide}, aspect_ratio="1:1",
            work_dir=root / "work-square", out_path=square,
            generator=MockGenerator(),
        )
        assert probe(square)["width"] == 1080 and probe(square)["height"] == 1080
        assert probe(square)["pix_fmt"] == "yuv420p"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.removeprefix("0x")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _run_render(client, project_id, uploaded):
    """Execute the render the upload already queued, or start one if it did not."""
    from app import jobs

    job = uploaded.get("job") or {}
    job_id = job.get("id") or uploaded.get("job_id")
    if not job_id:
        started = client.post(f"/api/projects/{project_id}/render", json={})
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
    jobs._execute(job_id, project_id)


def test_browser_create_upload_render_puts_the_image_in_the_mp4():
    """Same requests the create page sends: JSON project, then one photo.

    One photo is one scene for the whole duration. The finished MP4 is that
    photo, and the media URL still downloads it.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped browser image flow: ffmpeg not on PATH)")
        return

    from fastapi.testclient import TestClient

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
    scenes = body["scenes"]
    assert len(scenes) == 1
    assert scenes[0]["asset_url"]
    assert scenes[0]["duration"] == 6
    assert scenes[0]["caption"] == "a modern luxury living room"
    assert sum(s["duration"] for s in scenes) == 6
    assert body["status"] == "rendering"

    _run_render(client, project["id"], body)

    done = client.get(f"/api/projects/{project['id']}").json()
    assert done["status"] == "ready", done.get("error")
    video = root / done["video_url"].split("/media/", 1)[1]
    downloaded = client.get(done["video_url"])
    assert downloaded.status_code == 200, done["video_url"]
    assert downloaded.content == video.read_bytes()
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
    assert len(body["scenes"]) == 1
    assert body["scenes"][0]["duration"] == 6
    assert body["scenes"][0]["caption"] == "a teal bottle on a white table"
    assert all(s["asset_id"] in asset_ids and s["asset_url"] for s in body["scenes"])
    assert body["status"] == "rendering"

    _run_render(client, project["id"], body)

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
    """Four photos become four scenes. Each photo appears once in the MP4.

    A manual reassignment can still point one scene at another photo, and
    putting it back leaves the other scenes alone.
    """
    if not ffmpeg.ffmpeg_available():
        print("  (skipped multi-image reel: ffmpeg not on PATH)")
        return

    from fastapi.testclient import TestClient

    from app.main import app

    rooms = [
        ("Living Room", "0xE23B2F", lambda p: p[0] > 140 and p[0] > p[1] + 40 and p[0] > p[2] + 40),
        ("Dining Area", "0x1D4ED8", lambda p: p[2] > 140 and p[2] > p[0] + 40),
        ("Modular Kitchen", "0x16A34A", lambda p: p[1] > 100 and p[1] > p[0] + 40 and p[1] > p[2] + 20),
        ("Master Bedroom", "0xF59E0B", lambda p: p[0] > 160 and p[1] > 100 and p[2] < 80),
    ]

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

    uploaded_body = None
    for index, (name, color, _check) in enumerate(rooms):
        still = root / f"{name}.jpg"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=1280x720",
            "-frames:v", "1", str(still),
        ])
        uploaded = client.post(
            f"/api/projects/{project['id']}/uploads",
            params={"replan": "true" if index == len(rooms) - 1 else "false"},
            files={"file": (f"{name}.jpg", still.read_bytes(), "image/jpeg")},
        )
        assert uploaded.status_code == 201, uploaded.text
        uploaded_body = uploaded.json()

    scenes = sorted(uploaded_body["scenes"], key=lambda scene: scene["position"])
    images = [asset for asset in uploaded_body["assets"] if asset["kind"] == "image"]
    assert len(scenes) == 4
    assert [scene["asset_id"] for scene in scenes] == [asset["id"] for asset in images]
    assert len({scene["asset_id"] for scene in scenes}) == 4
    assert sum(scene["duration"] for scene in scenes) == 30
    assert scenes[0]["caption"] == "a house shown room by room"
    assert all(scene["caption"] is None for scene in scenes[1:])
    assert uploaded_body["status"] == "rendering"

    switched = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[0]['id']}/asset",
        json={"asset_id": scenes[2]["asset_id"]},
    )
    assert switched.status_code == 200, switched.text
    after = [s["asset_id"] for s in switched.json()["scenes"]]
    assert after == [scenes[2]["asset_id"], scenes[1]["asset_id"], scenes[2]["asset_id"], scenes[3]["asset_id"]]
    restored = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[0]['id']}/asset",
        json={"asset_id": scenes[0]["asset_id"]},
    )
    assert [s["asset_id"] for s in restored.json()["scenes"]] == [scene["asset_id"] for scene in scenes]

    _run_render(client, project["id"], uploaded_body)

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
    samples = []
    cursor = 0.0
    for room_index, scene in enumerate(scenes):
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
        samples.append(cursor + 0.8)
        cursor += scene["duration"]

    seen = []
    for at, room_index in zip(samples, range(4)):
        pixel = _rgb(video, at, "120:120:80:80")
        name, _color, check = rooms[room_index]
        print(f"  frame {at:.1f}s {name} rgb={tuple(round(c) for c in pixel)}")
        assert check(pixel), (at, name, pixel)
        seen.append(pixel)
        for color in ffmpeg.CARD_COLORS:
            card = _hex_rgb(color)
            distance = sum(abs(pixel[i] - card[i]) for i in range(3))
            assert distance > 80, (at, pixel, color)
    for earlier, later in ((0, 1), (1, 2), (2, 3)):
        gap = sum(abs(seen[earlier][i] - seen[later][i]) for i in range(3))
        assert gap > 80, (samples[earlier], samples[later], gap)
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
            params={"scene_id": scenes[0]["id"], "replan": "false"},
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
        _run_render(client, body["id"], started.json())

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
        # The move is visible. A frozen encode of this still is under 0.1;
        # the change sits on the color boundary, so the average stays small.
        assert _frame_delta(video, 0.25, scenes[0]["duration"] - 0.3) > 0.6

        # The next scene has no still, so it stays a title card.
        card_at = scenes[0]["duration"] + 0.4
        painted = _rgb(video, card_at, "80:80:40:80")
        expect = _hex_rgb(ffmpeg.CARD_COLORS[1])
        gap = sum(abs(painted[i] - expect[i]) for i in range(3))
        assert gap < 45, (painted, expect, gap)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_v32_motion_grade_transitions_and_export_presets_are_unchanged():
    """The picture path locked in V3.2. Typography may change; this may not."""
    from app import export_presets

    assert ffmpeg.COVER_SCALE == 1.12
    assert ffmpeg.FPS == 30
    assert ffmpeg.CROSSFADE_FRAMES == 16
    assert ffmpeg.MOTION_PATHS == (
        "push_in", "drift_lr", "diagonal", "pull_out", "drift_ud",
        "push_drift", "drift_rl", "drift_du", "pull_drift",
    )
    assert ffmpeg.TRANSITION_CYCLE == ("wipeleft",)
    assert [ffmpeg.transition_for(i) for i in range(8)] == ["wipeleft"] * 8
    assert ffmpeg._travel(1080, 1920, "wide") == (28, 38)
    assert ffmpeg._travel(1080, 1920, "tall") == (44, 22)
    assert ffmpeg._travel(1080, 1920, "unknown") == (34, 38)
    ease = "E"
    wide_zoom, wide_x, wide_y = ffmpeg._motion("push_in", ease, 1080, 1920, "wide")
    assert wide_zoom == "(1.030-0.020*E)"
    assert ")/2+(" in wide_x and ")/2+(" in wide_y
    plain_zoom, _, _ = ffmpeg._motion("push_in", ease, 1080, 1920, "unknown")
    assert plain_zoom == "(1.042-0.030*E)"
    held, drift_x, drift_y = ffmpeg._motion("drift_lr", ease, 1080, 1920, "wide")
    assert held == "1.020"
    assert "/2+(0)" in drift_y
    assert "-28.000+56.000*E" in drift_x

    quiet = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920, index=0,
    )
    picture = quiet[quiet.index("-vf") + 1]
    assert "drawtext" not in picture
    assert "eq=contrast=1.02:saturation=1.00:gamma=1.01" in picture
    assert "vignette=angle=PI/10" in picture
    assert "0.880" not in picture and "1.060" not in picture
    assert picture.startswith("scale=1210:2150:force_original_aspect_ratio=increase")

    assert export_presets.PRESETS["instagram_reel"].width == 1080
    assert export_presets.PRESETS["instagram_reel"].height == 1920
    assert export_presets.PRESETS["youtube_short"].width == 1080
    assert export_presets.PRESETS["youtube_short"].height == 1920
    assert export_presets.PRESETS["instagram_feed"].width == 1080
    assert export_presets.PRESETS["instagram_feed"].height == 1350
    assert export_presets.PRESETS["youtube_landscape"].width == 1920
    assert export_presets.PRESETS["youtube_landscape"].height == 1080
    assert all(preset.fps == 30 for preset in export_presets.PRESETS.values())
    assert all(preset.pix_fmt == "yuv420p" for preset in export_presets.PRESETS.values())
    for preset in export_presets.PRESETS.values():
        assert "1080" not in preset.label and "H.264" not in preset.label


def test_scene_text_wraps_on_word_boundaries_for_every_export_ratio():
    """Long lines wrap from the real font, and stay inside the side margin."""
    title = "Designed for a quieter kind of modern living with room to gather"
    subtitle = (
        "A private retreat crafted for memorable evenings and unhurried mornings"
    )
    title_font = ffmpeg.find_title_font()
    subtitle_font = ffmpeg.find_subtitle_font()
    assert title_font and subtitle_font
    frames = ((1080, 1920), (1080, 1350), (1080, 1080), (1920, 1080))
    for width, height in frames:
        title_block, subtitle_block = typography.compose(
            title, subtitle, title_font, subtitle_font, width, height,
        )
        assert title_block is not None and subtitle_block is not None
        limit = typography.max_line_width(width)
        for block, font in ((title_block, title_font), (subtitle_block, subtitle_font)):
            assert len(block.lines) <= 2, (width, height, block.lines)
            assert " ".join(block.lines) == (
                title if block is title_block else subtitle
            )
            for line in block.lines:
                assert " " not in line or line == line.strip()
                measured = typography.text_width(font, line, block.fontsize)
                assert measured <= limit + 0.51, (width, height, line, measured, limit)
        assert title_block.fontsize > subtitle_block.fontsize
        assert title_block.y.startswith("h*0.900-text_h-")
        assert subtitle_block.y == "h*0.900-text_h"
        # A short title stays on one line. The long one above had to wrap or shrink.
        short, _ = typography.compose(
            "Modern Luxury Living", "Designed for modern life",
            title_font, subtitle_font, width, height,
        )
        assert short is not None and short.lines == ("Modern Luxury Living",)

    sample = "Modern"
    measured = typography.text_width(title_font, sample, 36)
    assert 80 < measured < 280, measured
    narrow = typography.text_width(title_font, "i", 36)
    assert narrow < measured / 2

    cmd = ffmpeg.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=6, width=1080, height=1920,
        text_title=title, text_subtitle=subtitle,
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "\\n" in vf
    assert "box=1" not in vf
    assert "fontcolor=white@0.86" in vf


def test_forbidden_prompt_is_not_burned_and_end_card_is_optional():
    """A prompt that forbids type stays a photograph. A brand card is opt-in."""
    from app import render

    forbidden = {
        "id": "room", "title": "Room",
        "prompt": "warm interior, no text, no logo, no watermark, no caption",
        "duration": 2, "show_title": 1, "text_title": "PEARL INTERIORS",
        "show_subtitle": 1, "text_subtitle": "Your Vision",
    }
    assert render.visible_overlay(forbidden) == (None, None)
    spec = render.build_scene_spec(forbidden, 0, 1080, 1920, Path("room.jpg"), "standard")
    assert spec.text_title is None and spec.text_subtitle is None
    still = ffmpeg.build_still_clip_cmd(
        image="room.jpg", out="out.mp4", seconds=2, width=1080, height=1920,
        text_title=spec.text_title, text_subtitle=spec.text_subtitle,
    )
    assert "PEARL" not in still[still.index("-vf") + 1]
    allowed = {
        "prompt": "a quiet room with no texture specified",
        "show_title": 1, "text_title": "Hello",
        "show_subtitle": 0, "text_subtitle": "hidden",
    }
    assert render.visible_overlay(allowed) == ("Hello", None)
    assert render.configured_end_card([forbidden]) is None
    assert render.configured_end_card([{
        "end_card": {"title": "Pearl", "subtitle": "Design", "seconds": 3},
    }]) == ("Pearl", "Design", 3)
    assert render.configured_end_card([{"end_card": {"title": "  ", "subtitle": ""}}]) is None

    if not ffmpeg.ffmpeg_available():
        print("  (skipped end card and music render: ffmpeg not on PATH)")
        return

    from app.providers import MockGenerator

    root = Path(tempfile.mkdtemp(prefix="reelforge-brand-"))
    try:
        still_path = root / "room.jpg"
        ffmpeg.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=0xE23B2F:s=1280x720",
            "-frames:v", "1", str(still_path),
        ])
        music = root / "bed.wav"
        _media(music, 8, audio=True)
        scenes = [
            {
                "id": "s0", "title": "Room", "asset_id": "photo",
                "prompt": "warm interior, no text, no logo, no watermark",
                "duration": 2, "caption": None,
                "show_title": 1, "text_title": "PEARL INTERIORS",
            },
            {
                "id": "s1", "title": "Close", "asset_id": "photo-b",
                "prompt": "closing photograph",
                "duration": 2, "caption": None,
                "end_card": {"title": "PEARL INTERIORS", "subtitle": "Design", "seconds": 3},
            },
        ]
        images = {"s0": still_path, "s1": still_path}
        out = root / "with-music.mp4"
        render.render_reel(
            scenes=scenes, images=images, aspect_ratio="9:16",
            work_dir=root / "work", out_path=out, generator=MockGenerator(),
            audio=render.AudioSettings(music_path=music, volume=0.8, fade_out=1, fade_in=0.5),
        )
        duration = ffmpeg.probe_duration(str(out))
        assert duration is not None and abs(duration - 7) < 0.05, duration
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type,width,height,r_frame_rate",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "h264" in probe and "1080,1920,30/1" in probe, probe
        assert "audio" in probe
        photo = _rgb(out, 0.4, "80:80:40:80")
        assert photo[0] > 140 and photo[0] > photo[2] + 40, photo
        card = _rgb(out, 6.2, "80:80:40:80")
        expect = _hex_rgb(ffmpeg.CARD_COLORS[2])
        gap = sum(abs(card[i] - expect[i]) for i in range(3))
        assert gap < 45, (card, expect, gap)

        silent = root / "silent.mp4"
        quiet_scenes = [
            {"id": "q0", "title": "A", "prompt": "p", "duration": 2, "caption": None, "asset_id": "a"},
            {"id": "q1", "title": "B", "prompt": "p", "duration": 2, "caption": None, "asset_id": "b"},
        ]
        render.render_reel(
            scenes=quiet_scenes, images={"q0": still_path, "q1": still_path},
            aspect_ratio="9:16", work_dir=root / "quiet", out_path=silent,
            generator=MockGenerator(),
        )
        silent_streams = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(silent)],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "audio" not in silent_streams
        silent_duration = ffmpeg.probe_duration(str(silent))
        assert silent_duration is not None and abs(silent_duration - 4) < 0.05, silent_duration
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
