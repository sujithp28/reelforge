"""API-level checks: routes, status codes, and validation rules.

Runs against an isolated temporary data directory and with the job runner set
to "external", so no ffmpeg render is started — this file tests the HTTP
contract, not the pixels.

    python test_api.py
"""
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

def test_upload_image_attaches_to_every_bare_scene():
    project = make_project()
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert len(body["assets"]) == 1
    assert body["assets"][0]["kind"] == "image"
    assert all(s["asset_url"] for s in body["scenes"]), "still did not reach all scenes"
    # URLs are opaque to the frontend but must be servable paths, not disk paths.
    url = body["assets"][0]["url"]
    assert url.startswith(config.MEDIA_URL_PREFIX + "/uploads/"), url
    assert project["id"] in url


def test_upload_targets_one_scene_when_named():
    project = make_project()
    scene = project["scenes"][2]
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        params={"scene_id": scene["id"]},
        files={"file": ("ref.png", PNG_1PX, "image/png")},
    )
    assert res.status_code == 201, res.text
    scenes = {s["id"]: s for s in res.json()["scenes"]}
    assert scenes[scene["id"]]["asset_url"]
    assert not scenes[project["scenes"][0]["id"]]["asset_url"]


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

def test_audio_settings_round_trip():
    project = make_project()
    res = client.patch(
        f"/api/projects/{project['id']}/audio",
        json={"music_volume": 0.35, "music_fade_out": 4},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert abs(body["music_volume"] - 0.35) < 1e-6
    assert body["music_fade_out"] == 4

    assert client.patch(
        f"/api/projects/{project['id']}/audio", json={}
    ).status_code == 422
    assert client.patch(
        f"/api/projects/{project['id']}/audio", json={"music_volume": 9}
    ).status_code == 422


def test_audio_upload_is_stored_as_audio():
    project = make_project()
    res = client.post(
        f"/api/projects/{project['id']}/uploads",
        files={"file": ("track.mp3", b"ID3fake-but-nonempty", "audio/mpeg")},
    )
    assert res.status_code == 201, res.text
    kinds = [a["kind"] for a in res.json()["assets"]]
    assert kinds == ["audio"]
    # Audio must not be attached to scenes as a still.
    assert not any(s["asset_url"] for s in res.json()["scenes"])


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
    assert body["provider"] == "mock"
    job_id = body["job_id"]

    # The runner is external, so the job stays queued and the request returned
    # immediately — that is the separation this is checking.
    job = client.get(f"/api/projects/{project['id']}/jobs/{job_id}").json()
    assert job["status"] == "queued"
    assert job["provider"] == "mock"

    # A second render must not start while one is in flight.
    assert client.post(
        f"/api/projects/{project['id']}/render", json={}
    ).status_code == 409

    assert client.get(
        f"/api/projects/{project['id']}/jobs/job_nope"
    ).status_code == 404


def test_render_rejects_an_unconfigured_provider():
    if not ffmpeg.ffmpeg_available():
        print("  (skipped provider rejection test: ffmpeg not on PATH)")
        return
    project = make_project()
    # LTX is a declared integration boundary with no endpoint configured.
    res = client.post(f"/api/projects/{project['id']}/render", json={"provider": "ltx"})
    assert res.status_code == 503, res.text
    assert "ltx" in res.json()["detail"]
    # A rejected render must leave the project alone.
    assert client.get(f"/api/projects/{project['id']}").json()["status"] == "draft"

    bad = client.post(
        f"/api/projects/{project['id']}/render", json={"provider": "nope"}
    )
    assert bad.status_code == 422, bad.text


# --- deletion ---------------------------------------------------------------

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
    af = cmd[cmd.index("-af") + 1]
    assert "volume=0.250" in af, af
    assert "afade=t=out:st=27:d=3" in af, af
    assert cmd[cmd.index("-c:v") + 1] == "copy", "video must not be re-encoded"

    # A fade longer than the reel would produce a negative start time.
    short = ffmpeg.build_music_mux_cmd(
        video="v.mp4", music="m.mp3", out="o.mp4",
        total_seconds=2, volume=1.0, fade_out=5,
    )
    assert "afade" not in short[short.index("-af") + 1]


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
