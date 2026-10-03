"""API-level checks: routes, status codes, and validation rules.

Runs against an isolated temporary data directory and with the job runner set
to "external", so no ffmpeg render is started — this file tests the HTTP
contract, not the pixels.

    python test_api.py
"""
import json
import os
import shutil
import tempfile

# Must be set before importing app.config, which reads the environment once.
_TMP = tempfile.mkdtemp(prefix="reelforge-test-")
os.environ["REELFORGE_DATA_DIR"] = _TMP
os.environ["REELFORGE_JOB_RUNNER"] = "external"
os.environ["REELFORGE_VIDEO_PROVIDER"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, ffmpeg, providers, storage  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f9d0000000049454e44ae42"
    "6082"
)


def make_project(**overrides):
    body = {
        "idea": "a handmade leather wallet on oak",
        "category": "Product",
        "duration": 18,
        "aspect_ratio": "9:16",
    }
    body.update(overrides)
    res = client.post("/api/projects", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# --- health -----------------------------------------------------------------

def test_health_reports_the_wiring():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    for key in ("ffmpeg", "db_dialect", "storage", "job_runner", "video_provider"):
        assert key in body, key
    assert body["db_dialect"] == "sqlite"
    assert body["providers"]["mock"] is True


# --- project creation and validation ---------------------------------------

def test_create_project_plans_scenes():
    project = make_project()
    assert project["scenes"], "no scenes planned"
    assert sum(s["duration"] for s in project["scenes"]) == 18
    assert project["total_duration"] == 18
    # duration is derived from the scenes, so the two can never disagree.
    assert project["duration"] == project["total_duration"]
    assert project["status"] == "draft"
    assert project["video_url"] is None
    assert all("leather wallet" in s["prompt"] for s in project["scenes"][:1])


def test_create_project_rejects_bad_input():
    cases = [
        ({"idea": ""}, 422),
        ({"duration": 1}, 422),
        ({"duration": 500}, 422),
        ({"aspect_ratio": "3:2"}, 422),
    ]
    for override, expected in cases:
        body = {"idea": "x", "category": "Product", "duration": 18,
                "aspect_ratio": "9:16"}
        body.update(override)
        res = client.post("/api/projects", json=body)
        assert res.status_code == expected, (override, res.status_code, res.text)


def test_unknown_project_is_404():
    assert client.get("/api/projects/proj_does_not_exist").status_code == 404


def test_list_projects_paginates():
    before = client.get("/api/projects").json()["total"]
    make_project()
    body = client.get("/api/projects", params={"limit": 1}).json()
    assert body["total"] == before + 1
    assert len(body["projects"]) == 1
    assert "scene_count" in body["projects"][0]
    assert client.get("/api/projects", params={"limit": 0}).status_code == 422


# --- scene editing ----------------------------------------------------------

def test_update_scene_persists_and_resyncs_duration():
    project = make_project()
    scene = project["scenes"][1]
    other = project["total_duration"] - scene["duration"]

    res = client.patch(
        f"/api/projects/{project['id']}/scenes/{scene['id']}",
        json={"caption": "100% full-grain: no plastic", "duration": 5, "title": "Held"},
    )
    assert res.status_code == 200, res.text
    updated = res.json()
    edited = next(s for s in updated["scenes"] if s["id"] == scene["id"])
    assert edited["caption"] == "100% full-grain: no plastic"
    assert edited["title"] == "Held"
    assert edited["duration"] == 5
    assert updated["total_duration"] == other + 5
    assert updated["duration"] == updated["total_duration"], "duration drifted"


def test_export_preset_sets_the_frame_and_rejects_unknown_formats():
    bare = make_project()
    assert bare["export_preset"] is None
    assert bare["aspect_ratio"] == "9:16"

    feed = client.post("/api/projects", json={
        "idea": "a handmade leather wallet on oak",
        "category": "Product",
        "duration": 18,
        "aspect_ratio": "16:9",
        "export_preset": "instagram_feed",
        "width": 999,
        "height": 1,
    })
    assert feed.status_code == 201, feed.text
    saved = feed.json()
    assert saved["export_preset"] == "instagram_feed"
    assert saved["aspect_ratio"] == "4:5"
    assert "width" not in saved

    rejected = client.post("/api/projects", json={
        "idea": "x", "category": "Product", "duration": 18,
        "aspect_ratio": "9:16", "export_preset": "tiktok",
    })
    assert rejected.status_code == 422, rejected.text

    landscape = client.patch(
        f"/api/projects/{saved['id']}/export",
        json={"export_preset": "youtube_landscape"},
    )
    assert landscape.status_code == 200, landscape.text
    assert landscape.json()["export_preset"] == "youtube_landscape"
    assert landscape.json()["aspect_ratio"] == "16:9"

    short = client.patch(
        f"/api/projects/{bare['id']}/export",
        json={"export_preset": "youtube_short"},
    )
    assert short.status_code == 200, short.text
    assert short.json()["aspect_ratio"] == "9:16"
    assert short.json()["export_preset"] == "youtube_short"

    unknown = client.patch(
        f"/api/projects/{bare['id']}/export",
        json={"export_preset": "cinema"},
    )
    assert unknown.status_code == 422, unknown.text


def test_scene_text_is_saved_without_replacing_the_beat_title():
    project = make_project()
    scene = project["scenes"][0]
    assert scene["show_title"] is False
    assert scene["text_title"] is None
    res = client.patch(
        f"/api/projects/{project['id']}/scenes/{scene['id']}",
        json={
            "text_title": "Modern Luxury Living",
            "text_subtitle": "Designed for modern life",
            "show_title": True,
            "show_subtitle": True,
        },
    )
    assert res.status_code == 200, res.text
    edited = next(s for s in res.json()["scenes"] if s["id"] == scene["id"])
    assert edited["title"] == scene["title"]
    assert edited["text_title"] == "Modern Luxury Living"
    assert edited["text_subtitle"] == "Designed for modern life"
    assert edited["show_title"] is True and edited["show_subtitle"] is True
    off = client.patch(
        f"/api/projects/{project['id']}/scenes/{scene['id']}",
        json={"show_title": False},
    )
    assert off.status_code == 200, off.text
    hidden = next(s for s in off.json()["scenes"] if s["id"] == scene["id"])
    assert hidden["show_title"] is False
    assert hidden["text_title"] == "Modern Luxury Living"


def test_update_scene_rejects_a_total_over_the_maximum():
    project = make_project(duration=120)
    scene = project["scenes"][0]
    # Every scene is already near the cap, so pushing one to 30s must overflow.
    res = client.patch(
        f"/api/projects/{project['id']}/scenes/{scene['id']}", json={"duration": 30}
    )
    assert res.status_code == 422, res.text
    assert str(config.MAX_REEL_SECONDS) in res.json()["detail"]
    # The rejected write must not have been applied.
    after = client.get(f"/api/projects/{project['id']}").json()
    assert after["total_duration"] == project["total_duration"]


def test_update_scene_validation_and_404s():
    project = make_project()
    scene = project["scenes"][0]
    base = f"/api/projects/{project['id']}/scenes"
    assert client.patch(f"{base}/{scene['id']}", json={}).status_code == 422
    assert client.patch(f"{base}/{scene['id']}", json={"duration": 0}).status_code == 422
    assert client.patch(f"{base}/{scene['id']}", json={"duration": 99}).status_code == 422
    assert client.patch(f"{base}/scene_nope", json={"title": "x"}).status_code == 404
    assert client.patch(
        "/api/projects/proj_nope/scenes/scene_nope", json={"title": "x"}
    ).status_code == 404


# --- regeneration -----------------------------------------------------------

def test_regenerate_is_first_class_and_counted():
    project = make_project()
    scene = project["scenes"][1]
    url = f"/api/projects/{project['id']}/scenes/{scene['id']}/regenerate"

    first = client.post(url)
    assert first.status_code == 200, first.text
    one = next(s for s in first.json()["scenes"] if s["id"] == scene["id"])
    assert one["prompt"] != scene["prompt"]
    assert one["regen_count"] == 1

    second = client.post(url)
    two = next(s for s in second.json()["scenes"] if s["id"] == scene["id"])
    assert two["regen_count"] == 2
    # The attempt counter comes from its own column, so a prompt containing an
    # em-dash cannot knock the variation sequence off course.
    assert two["prompt"] != one["prompt"]


def test_regenerate_unknown_scene_is_404():
    project = make_project()
    res = client.post(f"/api/projects/{project['id']}/scenes/scene_nope/regenerate")
    assert res.status_code == 404


# --- uploads ----------------------------------------------------------------

def _upload_image_batch(project, count):
    """Upload `count` photos. Only the last request finishes the batch."""
    body = None
    for index in range(count):
        res = client.post(
            f"/api/projects/{project['id']}/uploads",
            params={"replan": "true" if index == count - 1 else "false"},
            files={"file": (f"photo-{index}.png", PNG_1PX, "image/png")},
        )
        assert res.status_code == 201, res.text
        body = res.json()
    return body


def _assert_one_scene_per_image(project, body, count):
    scenes = sorted(body["scenes"], key=lambda scene: scene["position"])
    images = [asset for asset in body["assets"] if asset["kind"] == "image"]
    assert len(scenes) == count, [scene["title"] for scene in scenes]
    assert len(images) == count
    assert [scene["asset_id"] for scene in scenes] == [asset["id"] for asset in images]
    assert len({scene["asset_id"] for scene in scenes}) == count
    assert images[-1]["id"] not in [scene["asset_id"] for scene in scenes[:-1]]
    assert sum(scene["duration"] for scene in scenes) == project["duration"]
    assert body["total_duration"] == project["duration"]
    assert scenes[0]["caption"] == project["idea"]
    assert all(scene["caption"] is None for scene in scenes[1:])
    assert all(scene["asset_url"] for scene in scenes)
    if ffmpeg.ffmpeg_available():
        assert body["status"] == "rendering", body.get("status")
        assert body["job"]["status"] == "queued"
    return scenes


def test_upload_image_becomes_one_scene():
    project = make_project()
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert len(body["assets"]) == 1
    assert body["assets"][0]["kind"] == "image"
    _assert_one_scene_per_image(project, body, 1)
    url = body["assets"][0]["url"]
    assert url.startswith(config.MEDIA_URL_PREFIX + "/uploads/"), url
    assert project["id"] in url


def test_uploaded_image_counts_match_scene_counts():
    """1, 3, 5 and 8 photos each become that many scenes. Nothing is repeated or dropped."""
    for count in (1, 3, 5, 8):
        project = make_project(
            idea="sunset over the harbour",
            category="Cinematic",
            duration=30,
            input_type="Image",
        )
        body = _upload_image_batch(project, count)
        _assert_one_scene_per_image(project, body, count)


def test_upload_targets_one_scene_when_the_batch_is_not_finished():
    project = make_project()
    scene = project["scenes"][2]
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        params={"scene_id": scene["id"], "replan": "false"},
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    assert res.status_code == 201, res.text
    scenes = {s["id"]: s for s in res.json()["scenes"]}
    assert scenes[scene["id"]]["asset_url"]
    assert not scenes[project["scenes"][0]["id"]]["asset_url"]
    assert res.json()["status"] == "draft"


def test_manual_scene_reassignment_still_changes_one_scene():
    project = make_project(category="Cinematic", duration=30, input_type="Image")
    scenes = project["scenes"]
    asset_ids = []
    for index, scene in enumerate(scenes[:4]):
        res = client.post(
            f"/api/projects/{project['id']}/uploads",
            params={"scene_id": scene["id"], "replan": "false"},
            files={"file": (f"room-{index}.png", PNG_1PX, "image/png")},
        )
        assert res.status_code == 201, res.text
        asset_ids.append(next(
            s["asset_id"] for s in res.json()["scenes"] if s["id"] == scene["id"]
        ))
    covered = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[4]['id']}/asset",
        json={"asset_id": asset_ids[3]},
    )
    assert covered.status_code == 200, covered.text

    changed = client.put(
        f"/api/projects/{project['id']}/scenes/{scenes[0]['id']}/asset",
        json={"asset_id": asset_ids[2]},
    )
    assert changed.status_code == 200, changed.text
    got = {s["id"]: s["asset_id"] for s in changed.json()["scenes"]}
    assert len(got) == len(scenes)
    assert got[scenes[0]["id"]] == asset_ids[2]
    assert got[scenes[1]["id"]] == asset_ids[1]
    assert got[scenes[2]["id"]] == asset_ids[2]
    assert got[scenes[3]["id"]] == asset_ids[3]
    assert got[scenes[4]["id"]] == asset_ids[3]


def test_assign_scene_asset_rejects_a_missing_image():
    project = make_project()
    scene = project["scenes"][0]
    missing = client.put(
        f"/api/projects/{project['id']}/scenes/{scene['id']}/asset",
        json={"asset_id": "asset_missing"},
    )
    assert missing.status_code == 404
    unknown_scene = client.put(
        f"/api/projects/{project['id']}/scenes/scene_nope/asset",
        json={"asset_id": "asset_missing"},
    )
    assert unknown_scene.status_code == 404

    other = make_project()
    uploaded = client.post(
        f"/api/projects/{other['id']}/uploads",
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    foreign = uploaded.json()["assets"][0]["id"]
    rejected = client.put(
        f"/api/projects/{project['id']}/scenes/{scene['id']}/asset",
        json={"asset_id": foreign},
    )
    assert rejected.status_code == 404
    untouched = client.get(f"/api/projects/{project['id']}").json()
    assert all(s["asset_id"] is None for s in untouched["scenes"])


def test_upload_rejects_bad_files():
    project = make_project()
    url = f"/api/projects/{project['id']}/uploads"
    assert client.post(
        url, files={"file": ("evil.exe", b"MZ", "application/x-msdownload")}
    ).status_code == 415
    assert client.post(
        url, files={"file": ("empty.png", b"", "image/png")}
    ).status_code == 422
    assert client.post(
        url, params={"scene_id": "scene_nope"},
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    ).status_code == 404
    assert client.post(
        "/api/projects/proj_nope/uploads",
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    ).status_code == 404


def test_upload_over_the_size_limit_is_413():
    project = make_project()
    oversized = b"\x00" * (config.MAX_UPLOAD_BYTES + 1024)
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("big.png", oversized, "image/png")},
    )
    assert res.status_code == 413, res.status_code


# --- audio settings ---------------------------------------------------------

def _wav_bytes() -> bytes:
    import io
    import wave
    tone = io.BytesIO()
    with wave.open(tone, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 1600)
    return tone.getvalue()


def test_audio_settings_round_trip():
    project = make_project()
    assert project["music_enabled"] is False
    assert project["music_fade_in"] == 1
    res = client.patch(
        f"/api/projects/{project['id']}/audio",
        json={"music_volume": 0.35, "music_fade_out": 4, "music_fade_in": 2, "music_enabled": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert abs(body["music_volume"] - 0.35) < 1e-6
    assert body["music_fade_out"] == 4
    assert body["music_fade_in"] == 2
    assert body["music_enabled"] is True
    again = client.get(f"/api/projects/{project['id']}")
    assert again.status_code == 200
    saved = again.json()
    assert saved["music_enabled"] is True
    assert saved["music_fade_in"] == 2
    assert abs(saved["music_volume"] - 0.35) < 1e-6

    assert client.patch(
        f"/api/projects/{project['id']}/audio", json={}
    ).status_code == 422
    assert client.patch(
        f"/api/projects/{project['id']}/audio", json={"music_volume": 9}
    ).status_code == 422
    assert client.patch(
        f"/api/projects/{project['id']}/audio", json={"music_fade_in": 11}
    ).status_code == 422


def test_audio_upload_is_stored_as_audio():
    project = make_project()
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("track.wav", _wav_bytes(), "audio/wav")},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    kinds = [a["kind"] for a in body["assets"]]
    assert kinds == ["audio"]
    # Audio must not be attached to scenes as a still.
    assert not any(s["asset_url"] for s in body["scenes"])
    assert body["music_enabled"] is True
    assert body["assets"][0]["url"]


def test_audio_upload_rejects_a_file_that_is_not_music():
    project = make_project()
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("track.mp3", b"ID3fake-but-nonempty", "audio/mpeg")},
    )
    assert res.status_code == 422, res.text
    detail = res.json()["detail"].lower()
    for word in ("ffmpeg", "codec", "bitrate", "ffprobe"):
        assert word not in detail
    kept = client.get(f"/api/projects/{project['id']}").json()
    assert kept["assets"] == []
    assert kept["music_enabled"] is False


def test_disabling_music_keeps_the_file_and_the_scene_clips():
    project = make_project()
    uploaded = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("track.wav", _wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 201, uploaded.text
    off = client.patch(
        f"/api/projects/{project['id']}/audio",
        json={"music_enabled": False},
    )
    assert off.status_code == 200, off.text
    body = off.json()
    assert body["music_enabled"] is False
    assert [a["kind"] for a in body["assets"]] == ["audio"]
    assert body["status"] == "draft"


def test_legacy_project_with_music_stays_enabled_after_migration():
    """A database from before the switch keeps music on, and a later off stays off."""
    import sqlite3
    import tempfile
    from app import db

    root = tempfile.mkdtemp(prefix="reelforge-music-mig-")
    conn = sqlite3.connect(root + "/old.db")
    conn.row_factory = sqlite3.Row
    try:
        for statement in db.SCHEMA:
            conn.execute(db.sql(statement))
        conn.execute(
            "INSERT INTO projects (id, title, idea, category, input_type, duration,"
            " aspect_ratio, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("keep", "t", "i", "Real Estate", "Idea", 30, "9:16", "t", "t"),
        )
        conn.execute(
            "INSERT INTO projects (id, title, idea, category, input_type, duration,"
            " aspect_ratio, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("quiet", "t", "i", "Real Estate", "Idea", 30, "9:16", "t", "t"),
        )
        conn.execute(
            "INSERT INTO assets (id, project_id, kind, filename, storage_key, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("a1", "keep", "audio", "song.mp3", "uploads/keep/a1.mp3", "t"),
        )
        added = db._migrate(conn)
        assert ("projects", "music_enabled") in added
        keep = conn.execute(
            "SELECT music_enabled, music_fade_in FROM projects WHERE id = 'keep'"
        ).fetchone()
        quiet = conn.execute(
            "SELECT music_enabled FROM projects WHERE id = 'quiet'"
        ).fetchone()
        assert keep["music_enabled"] == 1
        assert keep["music_fade_in"] == 1
        assert quiet["music_enabled"] == 0
        conn.execute("UPDATE projects SET music_enabled = 0 WHERE id = 'keep'")
        again = db._migrate(conn)
        assert ("projects", "music_enabled") not in again
        stuck = conn.execute(
            "SELECT music_enabled FROM projects WHERE id = 'keep'"
        ).fetchone()
        assert stuck["music_enabled"] == 0
    finally:
        conn.close()
        import shutil
        shutil.rmtree(root, ignore_errors=True)


# --- rendering: jobs are separate from the request -------------------------

def test_render_enqueues_a_job_without_running_it():
    if not ffmpeg.ffmpeg_available():
        print("  (skipped render enqueue test: ffmpeg not on PATH)")
        return
    project = make_project()
    res = client.post(f"/api/projects/{project['id']}/render", json={})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "rendering"
    assert "provider" not in body
    job_id = body["job_id"]

    # The runner is external, so the job stays queued and the request returned
    # immediately — that is the separation this is checking.
    job = client.get(f"/api/projects/{project['id']}/jobs/{job_id}").json()
    assert job["status"] == "queued"
    assert "provider" not in job
    from app import db, repo
    with db.connect() as conn:
        stored = repo.get_job(conn, job_id)
    assert stored["provider"] == "mock"

    # A second render must not start while one is in flight.
    assert client.post(
        f"/api/projects/{project['id']}/render", json={}
    ).status_code == 409

    assert client.get(
        f"/api/projects/{project['id']}/jobs/job_nope"
    ).status_code == 404


def test_client_cannot_override_the_configured_provider():
    """A customer body cannot select a provider, and errors name none."""
    if not ffmpeg.ffmpeg_available():
        print("  (skipped provider override test: ffmpeg not on PATH)")
        return
    from app import db, repo

    project = make_project()
    res = client.post(
        f"/api/projects/{project['id']}/render", json={"provider": "wan"}
    )
    assert res.status_code == 202, res.text
    lowered = res.text.lower()
    for leak in ("wan", "kaggle", "ltx", "mock"):
        assert leak not in lowered, leak
    assert "provider" not in res.json()
    with db.connect() as conn:
        stored = repo.get_job(conn, res.json()["job_id"])
    assert stored["provider"] == config.VIDEO_PROVIDER == "mock"
    project_body = client.get(f"/api/projects/{project['id']}").json()
    assert "provider" not in project_body["job"]

    # Even an unknown name is ignored. The server setting still wins.
    other = make_project()
    ignored = client.post(
        f"/api/projects/{other['id']}/render", json={"provider": "nope"}
    )
    assert ignored.status_code == 202, ignored.text
    with db.connect() as conn:
        stored = repo.get_job(conn, ignored.json()["job_id"])
    assert stored["provider"] == "mock"

    saved_provider = config.VIDEO_PROVIDER
    saved_key = config.LTX_API_KEY
    config.VIDEO_PROVIDER = "ltx"
    config.LTX_API_KEY = ""
    try:
        blocked = make_project()
        denied = client.post(
            f"/api/projects/{blocked['id']}/render", json={"provider": "wan"}
        )
        assert denied.status_code == 503, denied.text
        detail = denied.json()["detail"].lower()
        for leak in ("wan", "kaggle", "ltx", "mock"):
            assert leak not in detail, detail
        assert "couldn't be generated" in detail
        assert client.get(f"/api/projects/{blocked['id']}").json()["status"] == "draft"
    finally:
        config.VIDEO_PROVIDER = saved_provider
        config.LTX_API_KEY = saved_key


# --- deletion ---------------------------------------------------------------

def test_media_hides_the_database_and_still_serves_public_files():
    database = config.DATA_DIR / "reelforge.db"
    assert database.exists()
    hidden = client.get("/media/reelforge.db")
    assert hidden.status_code == 404
    assert b"SQLite format" not in hidden.content

    work = config.DATA_DIR / "work"
    work.mkdir(exist_ok=True)
    (work / "secret.txt").write_text("not-public", encoding="utf-8")
    assert client.get("/media/work/secret.txt").status_code == 404

    staging = config.DATA_DIR / "kaggle"
    staging.mkdir(exist_ok=True)
    (staging / "job.mp4").write_bytes(b"not-a-reel")
    assert client.get("/media/kaggle/job.mp4").status_code == 404

    project = make_project()
    uploaded = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    image = next(a for a in uploaded.json()["assets"] if a["kind"] == "image")
    preview = client.get(image["url"])
    assert preview.status_code == 200, image["url"]
    assert preview.content == PNG_1PX

    renders = config.DATA_DIR / "renders"
    renders.mkdir(exist_ok=True)
    (renders / "sample.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    download = client.get("/media/renders/sample.mp4")
    assert download.status_code == 200
    assert download.content.startswith(b"\x00\x00\x00\x18ftyp")

    clips = config.DATA_DIR / "clips" / "proj"
    clips.mkdir(parents=True, exist_ok=True)
    (clips / "scene.mp4").write_bytes(b"clip-bytes")
    assert client.get("/media/clips/proj/scene.mp4").content == b"clip-bytes"


def test_delete_removes_project_and_assets():
    project = make_project()
    client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    upload_dir = config.DATA_DIR / "uploads" / project["id"]
    assert upload_dir.exists()

    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
    assert not upload_dir.exists(), "uploaded files outlived the project"
    assert client.delete(f"/api/projects/{project['id']}").status_code == 404


# --- storage and provider abstractions -------------------------------------

def test_storage_keys_are_relative_and_traversal_is_refused():
    key = storage.upload_key("proj_x", "asset_y", ".jpg")
    assert key == "uploads/proj_x/asset_y.jpg"
    assert storage.render_key("proj_x") == "renders/proj_x.mp4"
    assert storage.storage.url_for(key) == f"{config.MEDIA_URL_PREFIX}/{key}"
    assert storage.storage.url_for(None) is None

    local = storage.LocalStorage(config.DATA_DIR)
    try:
        local.save_bytes("../escaped.txt", b"nope")
    except ValueError:
        pass
    else:
        raise AssertionError("a traversing storage key was accepted")


def test_browser_download_reads_the_project_file_and_not_a_downloads_folder():
    """The website only offers the project file. It does not write to Downloads."""
    project = make_project()
    payload = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
    key = storage.render_key(project["id"])
    storage.storage.save_bytes(key, payload)
    from app import db
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET video_key = ?, status = ?, output_stale = 0 WHERE id = ?",
            (key, "ready", project["id"]),
        )
    body = client.get(f"/api/projects/{project['id']}").json()
    assert body["video_url"] == f"/media/{key}"
    assert "downloads" not in body
    fetched = client.get(body["video_url"])
    assert fetched.status_code == 200
    assert fetched.content == payload
    outside = client.get("/media/../reelforge.db")
    assert outside.status_code in (404, 400)


def test_storage_round_trip():
    local = storage.LocalStorage(config.DATA_DIR)
    key = local.save_bytes("uploads/proj_t/file.txt", b"hello")
    path = local.localize(key)
    assert path is not None and path.read_bytes() == b"hello"
    assert local.localize("uploads/proj_t/missing.txt") is None
    local.delete_prefix("uploads/proj_t")
    assert local.localize(key) is None


def test_provider_registry():
    mock = providers.build_generator("mock")
    assert mock.name == "mock" and mock.available()

    ltx = providers.build_generator("ltx")
    assert ltx.name == "ltx"
    # Declared but unavailable until an endpoint is configured — that is the
    # whole point of the stub.
    assert ltx.available() is False
    try:
        ltx.generate(None, None)  # type: ignore[arg-type]
    except ffmpeg.RenderError:
        pass
    else:
        raise AssertionError("the LTX stub pretended to render")

    try:
        providers.build_generator("wat")
    except ffmpeg.RenderError:
        pass
    else:
        raise AssertionError("an unknown provider was accepted")


def test_quality_is_customer_facing_only():
    project = make_project()
    assert project["quality"] == "standard"

    res = client.patch(f"/api/projects/{project['id']}/quality",
                       json={"quality": "high"})
    assert res.status_code == 200, res.text
    assert res.json()["quality"] == "high"

    # No model identifier is ever returned at the API boundary. Checked
    # against the real configured model names, not generic words like "pro"
    # which legitimately appear inside "prompt" and "Product".
    body = json.dumps(res.json()).lower()
    for leak in ("ltx-2-5", config.LTX_MODEL_STANDARD, config.LTX_MODEL_HIGH):
        assert leak.lower() not in body, leak

    assert client.patch(f"/api/projects/{project['id']}/quality",
                        json={"quality": "ultra"}).status_code == 422
    assert client.patch(f"/api/projects/{project['id']}/quality",
                        json={"quality": "ltx-2-5-pro"}).status_code == 422


def test_quality_can_be_chosen_at_creation():
    project = make_project(quality="high")
    assert project["quality"] == "high"
    res = client.post("/api/projects", json={
        "idea": "x", "category": "Product", "duration": 18,
        "aspect_ratio": "9:16", "quality": "nonsense",
    })
    assert res.status_code == 422


def test_scene_retry_enqueues_a_job_for_one_scene():
    if not ffmpeg.ffmpeg_available():
        print("  (skipped scene retry test: ffmpeg not on PATH)")
        return
    project = make_project()
    scene = project["scenes"][1]

    res = client.post(f"/api/projects/{project['id']}/scenes/{scene['id']}/retry")
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["scene_id"] == scene["id"]
    assert body["status"] == "rendering"
    assert body["job_id"]

    # Retrying an unknown scene must not enqueue anything.
    assert client.post(
        f"/api/projects/{project['id']}/scenes/scene_nope/retry"
    ).status_code == 404
    # And not while a render is already in flight.
    assert client.post(
        f"/api/projects/{project['id']}/scenes/{scene['id']}/retry"
    ).status_code == 409


def test_scene_edit_clears_only_that_scenes_clip():
    """Editing one scene must not invalidate the others' cached clips."""
    from app import db, repo

    project = make_project()
    scenes = project["scenes"]
    with db.connect() as conn:
        for i, s in enumerate(scenes):
            repo.set_clip_ready(
                conn, s["id"], key=f"clips/{project['id']}/{s['id']}_h{i}.mp4",
                clip_hash=f"h{i}", provider="mock",
            )

    target = scenes[1]
    client.patch(f"/api/projects/{project['id']}/scenes/{target['id']}",
                 json={"prompt": "a different prompt"})

    after = {s["id"]: s for s in
             client.get(f"/api/projects/{project['id']}").json()["scenes"]}
    assert after[target["id"]]["clip_status"] == "pending", "edited scene kept its clip"
    for s in scenes:
        if s["id"] != target["id"]:
            assert after[s["id"]]["clip_status"] == "ready", (
                f"scene {s['id']} lost its clip because a sibling was edited"
            )


def test_quality_change_clears_every_clip():
    """Quality changes the pixels of every scene, so none can be reused."""
    from app import db, repo

    project = make_project()
    with db.connect() as conn:
        for i, s in enumerate(project["scenes"]):
            repo.set_clip_ready(conn, s["id"], key=f"clips/x/{s['id']}.mp4",
                                clip_hash=f"h{i}", provider="mock")

    client.patch(f"/api/projects/{project['id']}/quality", json={"quality": "high"})
    after = client.get(f"/api/projects/{project['id']}").json()["scenes"]
    assert all(s["clip_status"] == "pending" for s in after), after


def test_clip_keys_are_not_exposed_to_the_browser():
    from app import db, repo

    project = make_project()
    with db.connect() as conn:
        repo.set_clip_ready(conn, project["scenes"][0]["id"],
                            key="clips/secret/internal_path.mp4",
                            clip_hash="hash-not-for-the-browser",
                            provider="provider-not-for-the-browser")
    body = client.get(f"/api/projects/{project['id']}").text
    assert "internal_path" not in body
    assert "clip_key" not in body
    assert "clip_hash" not in body
    assert "clip_provider" not in body
    assert "hash-not-for-the-browser" not in body
    assert "provider-not-for-the-browser" not in body


def test_cancel_a_queued_job():
    if not ffmpeg.ffmpeg_available():
        print("  (skipped cancel test: ffmpeg not on PATH)")
        return
    project = make_project()
    started = client.post(f"/api/projects/{project['id']}/render", json={}).json()
    job_id = started["job_id"]

    res = client.post(f"/api/projects/{project['id']}/jobs/{job_id}/cancel")
    assert res.status_code == 202, res.text
    assert res.json()["cancel_requested"] is True

    job = client.get(f"/api/projects/{project['id']}/jobs/{job_id}").json()
    assert job["cancel_requested"] == 1

    assert client.post(
        f"/api/projects/{project['id']}/jobs/job_nope/cancel"
    ).status_code == 404


def test_generation_log_records_attempts_without_secrets():
    from app import db, repo

    project = make_project()
    scene = project["scenes"][0]
    with db.connect() as conn:
        repo.log_generation(
            conn, project_id=project["id"], scene_id=scene["id"], job_id="job_x",
            provider="ltx", attempt=1, status="failed", seconds=8,
            elapsed_ms=4200, error="the video service could not generate this scene",
        )
    body = client.get(f"/api/projects/{project['id']}/generations")
    assert body.status_code == 200
    rows = body.json()["generations"]
    assert len(rows) == 1
    row = rows[0]
    # Every field the cost/audit requirement asks for.
    for field in ("project_id", "scene_id", "provider", "attempt", "status",
                  "seconds", "created_at", "error"):
        assert field in row, field
    assert row["provider"] == "ltx" and row["attempt"] == 1
    # And nothing resembling a credential or endpoint.
    raw = json.dumps(rows).lower()
    for leak in ("bearer", "api_key", "authorization", "api.ltx.io"):
        assert leak not in raw, leak


def test_restart_releases_orphaned_renders():
    """A killed server must not leave a project spinning at 'rendering'."""
    from app import db, repo

    project = make_project()
    with db.connect() as conn:
        assert repo.claim_for_render(conn, project["id"]) is True
        # Second claim must fail: that is the atomic guard against two renders.
        assert repo.claim_for_render(conn, project["id"]) is False
    assert client.get(f"/api/projects/{project['id']}").json()["status"] == "rendering"

    with db.connect() as conn:
        released = repo.release_stale_renders(conn)
    assert released >= 1
    after = client.get(f"/api/projects/{project['id']}").json()
    assert after["status"] == "failed"
    assert "restarted" in after["error"]
    # And it must be renderable again rather than wedged.
    with db.connect() as conn:
        assert repo.claim_for_render(conn, project["id"]) is True


def test_queued_jobs_are_visible_to_an_external_worker():
    """The API only enqueues, so a separate process can find the work."""
    from app import db, repo

    if not ffmpeg.ffmpeg_available():
        print("  (skipped external worker test: ffmpeg not on PATH)")
        return
    project = make_project()
    client.post(f"/api/projects/{project['id']}/render", json={})
    with db.connect() as conn:
        queued = repo.claim_queued_jobs(conn, limit=50)
    assert any(j["project_id"] == project["id"] for j in queued), queued


def test_music_mux_command_uses_the_audio_settings():
    cmd = ffmpeg.build_music_mux_cmd(
        video="v.mp4", music="m.mp3", out="o.mp4",
        total_seconds=30, volume=0.25, fade_out=3,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=0.250" in graph, graph
    assert "afade=t=in:st=0:d=1.000" in graph, graph
    assert "afade=t=out:st=27.000:d=3" in graph, graph
    assert "atrim=end=30.000" in graph and "apad" in graph
    assert "-shortest" not in cmd
    assert cmd[cmd.index("-t") + 1] == "30.000"
    assert cmd[cmd.index("-c:v") + 1] == "copy", "video must not be re-encoded"

    custom = ffmpeg.build_music_mux_cmd(
        video="v.mp4", music="m.mp3", out="o.mp4",
        total_seconds=30, volume=1.0, fade_out=2, fade_in=0.8,
    )
    custom_graph = custom[custom.index("-filter_complex") + 1]
    assert "afade=t=in:st=0:d=0.800" in custom_graph

    # A fade longer than the reel would produce a negative start time.
    short = ffmpeg.build_music_mux_cmd(
        video="v.mp4", music="m.mp3", out="o.mp4",
        total_seconds=2, volume=1.0, fade_out=5,
    )
    assert "afade" not in short[short.index("-filter_complex") + 1]


def test_reorder_keeps_titles_with_their_photos():
    project = make_project()
    scenes = project["scenes"]
    assert len(scenes) >= 2
    first, second = scenes[0], scenes[1]
    titled = client.patch(
        f"/api/projects/{project['id']}/scenes/{first['id']}",
        json={"text_title": "Living room", "show_title": True},
    )
    assert titled.status_code == 200, titled.text
    titled = client.patch(
        f"/api/projects/{project['id']}/scenes/{second['id']}",
        json={"text_title": "Kitchen", "show_title": True, "text_subtitle": "Morning light", "show_subtitle": True},
    )
    assert titled.status_code == 200, titled.text
    for scene, name in ((first, "living.png"), (second, "kitchen.png")):
        uploaded = client.post(
            f"/api/projects/{project['id']}/uploads",
            files={"file": (name, PNG_1PX, "image/png")},
            params={"scene_id": scene["id"], "replan": "false"},
        )
        assert uploaded.status_code == 201, uploaded.text
    current = client.get(f"/api/projects/{project['id']}").json()
    by_id = {scene["id"]: scene for scene in current["scenes"]}
    ids = [scene["id"] for scene in current["scenes"]]
    ids[0], ids[1] = ids[1], ids[0]
    moved = client.put(
        f"/api/projects/{project['id']}/scenes/order",
        json={"scene_ids": ids},
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert [scene["id"] for scene in body["scenes"]] == ids
    assert body["scenes"][0]["text_title"] == by_id[ids[0]]["text_title"]
    assert body["scenes"][0]["text_subtitle"] == by_id[ids[0]]["text_subtitle"]
    assert body["scenes"][0]["asset_id"] == by_id[ids[0]]["asset_id"]
    assert body["output_stale"] is True
    assert body["music_enabled"] == current["music_enabled"]
    assert body["music_volume"] == current["music_volume"]
    rejected = client.put(
        f"/api/projects/{project['id']}/scenes/order",
        json={"scene_ids": ids[:1]},
    )
    assert rejected.status_code == 422


def test_removing_a_photo_keeps_the_scene_and_the_previous_reel():
    from app import db

    project = make_project()
    scene = project["scenes"][0]
    uploaded = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("room.png", PNG_1PX, "image/png")},
        params={"scene_id": scene["id"], "replan": "false"},
    )
    assert uploaded.status_code == 201, uploaded.text
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET video_key = ?, status = ?, output_stale = 0 WHERE id = ?",
            ("renders/keep.mp4", "ready", project["id"]),
        )
    removed = client.delete(f"/api/projects/{project['id']}/scenes/{scene['id']}/asset")
    assert removed.status_code == 200, removed.text
    body = removed.json()
    kept = next(item for item in body["scenes"] if item["id"] == scene["id"])
    assert kept["asset_id"] is None
    assert kept["text_title"] == scene["text_title"]
    assert body["video_url"].endswith("renders/keep.mp4")
    assert body["output_stale"] is True
    assert len(body["scenes"]) == len(project["scenes"])


def test_failed_render_keeps_the_previous_reel():
    from app import db, repo

    project = make_project()
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET video_key = ?, status = ?, output_stale = 0 WHERE id = ?",
            ("renders/keep.mp4", "ready", project["id"]),
        )
        repo.mark_failed(conn, project["id"], "The reel could not be finished.")
    body = client.get(f"/api/projects/{project['id']}").json()
    assert body["status"] == "failed"
    assert body["error"] == "The reel could not be finished."
    assert body["video_url"].endswith("renders/keep.mp4")


def test_rename_does_not_mark_the_reel_stale():
    from app import db

    project = make_project()
    with db.connect() as conn:
        conn.execute(
            "UPDATE projects SET video_key = ?, status = ?, output_stale = 0 WHERE id = ?",
            ("renders/keep.mp4", "ready", project["id"]),
        )
    renamed = client.patch(f"/api/projects/{project['id']}", json={"title": "Harbour house"})
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["title"] == "Harbour house"
    assert body["status"] == "ready"
    assert body["output_stale"] is False
    assert body["video_url"].endswith("renders/keep.mp4")
    blank = client.patch(f"/api/projects/{project['id']}", json={"title": "   "})
    assert blank.status_code == 422


def test_add_and_remove_scene_keeps_music_and_the_other_scenes():
    project = make_project()
    music = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("tone.wav", _wav_bytes(), "audio/wav")},
    )
    assert music.status_code == 201, music.text
    before = music.json()
    added = client.post(f"/api/projects/{project['id']}/scenes", json={})
    assert added.status_code == 201, added.text
    body = added.json()
    assert len(body["scenes"]) == len(before["scenes"]) + 1
    assert body["music_enabled"] is True
    assert body["music_volume"] == before["music_volume"]
    extra = body["scenes"][-1]
    assert extra["title"].startswith("Scene")
    removed = client.delete(f"/api/projects/{project['id']}/scenes/{extra['id']}")
    assert removed.status_code == 200, removed.text
    after = removed.json()
    assert len(after["scenes"]) == len(before["scenes"])
    assert [scene["id"] for scene in after["scenes"]] == [scene["id"] for scene in before["scenes"]]
    assert after["music_enabled"] is True
    body = after
    while len(body["scenes"]) > 2:
        last = body["scenes"][-1]
        if body["total_duration"] - last["duration"] < 5:
            break
        deleted = client.delete(f"/api/projects/{project['id']}/scenes/{last['id']}")
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
    assert len(body["scenes"]) == 2, [scene["duration"] for scene in body["scenes"]]
    for scene in sorted(body["scenes"], key=lambda item: item["duration"]):
        edited = client.patch(
            f"/api/projects/{project['id']}/scenes/{scene['id']}",
            json={"duration": 3},
        )
        assert edited.status_code == 200, edited.text
    blocked = client.delete(
        f"/api/projects/{project['id']}/scenes/{body['scenes'][1]['id']}"
    )
    assert blocked.status_code == 422, blocked.text
    still = client.get(f"/api/projects/{project['id']}").json()
    assert any(scene["id"] == body["scenes"][1]["id"] for scene in still["scenes"])
    assert still["music_enabled"] is True


def test_append_photo_adds_a_scene_without_replacing_the_others():
    project = make_project()
    original = [scene["id"] for scene in project["scenes"]]
    added = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("extra.png", PNG_1PX, "image/png")},
        params={"append": "true", "replan": "false"},
    )
    assert added.status_code == 201, added.text
    body = added.json()
    assert [scene["id"] for scene in body["scenes"][:-1]] == original
    assert body["scenes"][-1]["asset_url"]
    assert body["output_stale"] is True


def test_export_preset_frames_match_the_studio():
    from app import export_presets

    expected = {
        "instagram_reel": ("9:16", 1080, 1920, 30, "Instagram Reel"),
        "youtube_short": ("9:16", 1080, 1920, 30, "YouTube Short"),
        "instagram_feed": ("4:5", 1080, 1350, 30, "Instagram Feed"),
        "youtube_landscape": ("16:9", 1920, 1080, 30, "YouTube"),
    }
    assert set(export_presets.PRESETS) == set(expected)
    for preset_id, (ratio, width, height, fps, label) in expected.items():
        preset = export_presets.PRESETS[preset_id]
        assert (preset.aspect_ratio, preset.width, preset.height, preset.fps, preset.label) == (
            ratio, width, height, fps, label,
        )
        assert "1080" not in preset.label
        assert "H.264" not in preset.label


def test_save_file_replaces_without_dropping_the_previous_file_on_a_failed_write(tmp_path=None):
    """A finished replace swaps the file. The previous bytes survive until then."""
    from pathlib import Path
    from app import storage

    root = Path(_TMP) / "replace-check"
    root.mkdir(exist_ok=True)
    local = storage.LocalStorage(root)
    previous = root / "src-old.mp4"
    previous.write_bytes(b"old-reel")
    local.save_file("renders/proj.mp4", previous)
    assert (root / "renders" / "proj.mp4").read_bytes() == b"old-reel"
    nxt = root / "src-new.mp4"
    nxt.write_bytes(b"new-reel")
    local.save_file("renders/proj.mp4", nxt)
    assert (root / "renders" / "proj.mp4").read_bytes() == b"new-reel"
    assert not (root / "renders" / "proj.mp4.partial").exists()


def test_luxury_interiors_creates_five_scenes_and_both_scripts():
    project = make_project(
        idea="a quiet living room at dusk",
        category="Luxury Interiors",
        duration=30,
        language="te",
        brand_name="North Room",
    )
    assert len(project["scenes"]) == 5
    assert [scene["title"] for scene in project["scenes"]] == [
        "Opening hook", "Interior inspiration", "Design detail",
        "Practical idea", "Branded close",
    ]
    assert sum(scene["duration"] for scene in project["scenes"]) == 30
    assert project["export_preset"] == "instagram_reel"
    assert project["aspect_ratio"] == "9:16"
    assert project["music_enabled"] is False
    assert project["brand_name"] == "North Room"
    script = project["content_script"]
    assert script["template"] == "luxury_interiors"
    assert script["language"] == "te"
    assert len(script["english"]["scenes"]) == 5
    assert len(script["telugu"]["scenes"]) == 5
    assert script["instagram_caption"]
    assert script["youtube_description"]
    assert script["hashtags"]
    assert project["scenes"][0]["text_title"]
    assert project["scenes"][4]["text_title"] == "North Room"
    from app.luxury import contains_invented_claim
    assert contains_invented_claim(script) is None
    plain = make_project()
    assert plain["content_script"] is None


def test_luxury_script_edits_persist_and_other_projects_refuse_them():
    project = make_project(
        idea="morning light", category="Luxury Interiors", duration=30,
    )
    assert project["brand_name"] == "Luxury Living Studio"
    english = project["content_script"]["english"]
    english["hook"] = "Start with the room"
    english["scenes"][0]["title"] = "Open with light"
    english["cta"] = "Save this idea"
    saved = client.put(f"/api/projects/{project['id']}/luxury-script", json={
        "language": "en",
        "english": english,
        "instagram_caption": "A calm idea for these pictures.",
        "youtube_description": "A short idea.\nNo prices.",
        "hashtags": ["#QuietRooms"],
        "apply_to_scenes": True,
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["scenes"][0]["text_title"] == "Open with light"
    assert body["content_script"]["instagram_caption"] == "A calm idea for these pictures."
    assert body["content_script"]["english"]["hook"] == "Start with the room"
    assert body["content_script"]["telugu"]["scenes"]
    other = make_project()
    refused = client.put(
        f"/api/projects/{other['id']}/luxury-script",
        json={"topic": "morning light"},
    )
    assert refused.status_code == 422


def test_luxury_reuses_an_existing_image_and_keeps_the_five_scenes():
    project = make_project(
        idea="evening room", category="Luxury Interiors", duration=30,
    )
    titles = [scene["text_title"] for scene in project["scenes"]]
    for index in range(3):
        replan = "true" if index == 2 else "false"
        uploaded = client.post(
            f"/api/projects/{project['id']}/uploads?replan={replan}",
            files={"file": (f"room{index}.png", PNG_1PX, "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert len(body["scenes"]) == 5
    assert [scene["text_title"] for scene in body["scenes"]] == titles
    assert body["status"] == "draft"
    images = [asset for asset in body["assets"] if asset["kind"] == "image"]
    assert len(images) == 3
    assert [scene["asset_id"] for scene in body["scenes"][:3]] == [item["id"] for item in images]
    assert body["scenes"][3]["asset_id"] is None
    scene_id = body["scenes"][3]["id"]
    assigned = client.put(
        f"/api/projects/{project['id']}/scenes/{scene_id}/asset",
        json={"asset_id": images[0]["id"]},
    )
    assert assigned.status_code == 200, assigned.text
    again = assigned.json()
    assert len([asset for asset in again["assets"] if asset["kind"] == "image"]) == 3
    assert again["scenes"][3]["asset_id"] == images[0]["id"]
    assert again["scenes"][0]["asset_id"] == images[0]["id"]


def test_luxury_logo_is_not_placed_on_a_scene():
    project = make_project(
        idea="evening room", category="Luxury Interiors", duration=30,
    )
    before = [scene["asset_id"] for scene in project["scenes"]]
    logo = client.post(
        f"/api/projects/{project['id']}/logo",
        files={"file": ("mark.png", PNG_1PX, "image/png")},
    )
    assert logo.status_code == 201, logo.text
    body = logo.json()
    assert body["logo_url"]
    assert [scene["asset_id"] for scene in body["scenes"]] == before
    assert [asset["kind"] for asset in body["assets"]] == ["logo"]
    assert body["output_stale"] is False


def test_luxury_image_prompts_are_five_local_cards():
    project = make_project(
        idea="a quiet living room at dusk",
        category="Luxury Interiors",
        duration=30,
        language="en",
    )
    prompts = project["content_script"]["image_prompts"]
    assert prompts["format"].startswith("Portrait 9:16")
    assert prompts["instruction"].startswith("Copy each prompt and generate its image")
    assert len(prompts["scenes"]) == 5
    assert [scene["role"] for scene in prompts["scenes"]] == [
        "Opening hook", "Interior inspiration", "Design details",
        "Practical idea", "Branded close",
    ]
    assert all("quiet living room" in scene["prompt"] for scene in prompts["scenes"])
    assert all("9:16" in scene["prompt"] for scene in prompts["scenes"])
    assert all("no watermark" in scene["prompt"] for scene in prompts["scenes"])
    assert "luxury home" in prompts["scenes"][0]["prompt"]
    assert "living room" in prompts["scenes"][1]["prompt"]
    assert "marble" in prompts["scenes"][2]["prompt"]
    assert "practical" in prompts["scenes"][3]["prompt"].lower()
    assert "brand ending" in prompts["scenes"][4]["prompt"]
    from app.luxury import contains_invented_claim
    assert contains_invented_claim(project["content_script"]) is None
    claimed = json.loads(json.dumps(project["content_script"]))
    claimed["english"]["scenes"][0]["title"] = "Italian marble"
    assert contains_invented_claim(claimed) == "marble"
    telugu = make_project(
        idea="a quiet living room at dusk",
        category="Luxury Interiors",
        duration=30,
        language="te",
    )
    telugu_prompt = telugu["content_script"]["image_prompts"]["scenes"][0]["prompt"]
    assert "9:16" in telugu_prompt
    assert "గది" in telugu_prompt
    original = prompts["scenes"][0]["prompt"]
    kept = prompts["scenes"][1]["prompt"]
    saved = client.put(f"/api/projects/{project['id']}/luxury-script", json={
        "regenerate_image_prompt": 0,
        "apply_to_scenes": False,
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    changed = body["content_script"]["image_prompts"]["scenes"]
    assert changed[0]["prompt"] != original
    assert changed[1]["prompt"] == kept
    assert body["scenes"][0]["text_title"] == project["scenes"][0]["text_title"]
    edited = body["content_script"]["image_prompts"]
    edited["scenes"][2]["prompt"] = "Custom portrait prompt for scene three, 9:16."
    again = client.put(f"/api/projects/{project['id']}/luxury-script", json={
        "image_prompts": edited,
        "apply_to_scenes": False,
    })
    assert again.status_code == 200, again.text
    assert again.json()["content_script"]["image_prompts"]["scenes"][2]["prompt"].startswith(
        "Custom portrait"
    )
    scene = project["scenes"][2]
    uploaded = client.post(
        f"/api/projects/{project['id']}/uploads?scene_id={scene['id']}&replan=false",
        files={"file": ("detail.png", PNG_1PX, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assigned = uploaded.json()
    assert len(assigned["scenes"]) == 5
    assert assigned["scenes"][2]["asset_id"]
    assert assigned["scenes"][0]["asset_id"] is None
    assert assigned["scenes"][1]["asset_id"] is None
    plain = make_project()
    assert plain["content_script"] is None
    assert plain["category"] == "Product"


def test_luxury_image_generation_stays_off():
    body = client.get("/api/luxury/capabilities").json()
    assert body["available"] is False
    assert body["message"].startswith("Copy each prompt and generate its image")


def test_luxury_render_requires_each_scene_image():
    if not ffmpeg.ffmpeg_available():
        print("  (skipped luxury render test: ffmpeg not on PATH)")
        return
    project = make_project(
        idea="a quiet hallway", category="Luxury Interiors", duration=10,
    )
    blocked = client.post(f"/api/projects/{project['id']}/render", json={})
    assert blocked.status_code == 422, blocked.text
    assert "Scenes 1, 2, 3, 4, 5" in blocked.json()["detail"]
    assert "before rendering" in blocked.json()["detail"]

    first_id = None
    for scene in project["scenes"]:
        uploaded = client.post(
            f"/api/projects/{project['id']}/uploads?scene_id={scene['id']}&replan=false",
            files={"file": (f"{scene['position']}.png", PNG_1PX, "image/png")},
        )
        assert uploaded.status_code == 201, uploaded.text
        project = uploaded.json()
        if first_id is None:
            first_id = project["scenes"][0]["asset_id"]
    assert all(scene["asset_id"] for scene in project["scenes"])
    images = [asset for asset in project["assets"] if asset["kind"] == "image"]
    assert len(images) == 5

    replaced = client.post(
        f"/api/projects/{project['id']}/uploads?scene_id={project['scenes'][0]['id']}&replan=false",
        files={"file": ("replacement.png", PNG_1PX, "image/png")},
    )
    assert replaced.status_code == 201, replaced.text
    project = replaced.json()
    assert project["scenes"][0]["asset_id"] != first_id
    library = [asset["id"] for asset in project["assets"] if asset["kind"] == "image"]
    assert first_id in library
    assert len(library) == 6
    reused = client.put(
        f"/api/projects/{project['id']}/scenes/{project['scenes'][0]['id']}/asset",
        json={"asset_id": first_id},
    )
    assert reused.status_code == 200, reused.text
    project = reused.json()
    assert project["scenes"][0]["asset_id"] == first_id
    assert len([asset for asset in project["assets"] if asset["kind"] == "image"]) == 6

    res = client.post(f"/api/projects/{project['id']}/render", json={})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "rendering"
    job = client.get(f"/api/projects/{project['id']}/jobs/{body['job_id']}").json()
    assert job["status"] == "queued"


def demo():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} API checks passed")


if __name__ == "__main__":
    try:
        demo()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
