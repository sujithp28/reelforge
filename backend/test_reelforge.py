"""Self-check for the parts with real logic: duration maths and ffmpeg command
building. Plain asserts, no test framework.

    python test_reelforge.py
"""
from app import render, storyboard


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
    out = render.escape_drawtext("Price: 20% off, it's [real]\nsecond line")
    for ch in (":", "%", ",", "[", "]"):
        assert "\\" + ch in out, (ch, out)
    # Apostrophes cannot be escaped inside a single-quoted value at all.
    assert "'" not in out
    assert "’" in out
    assert "\n" not in out


def test_font_is_discoverable():
    # Captions are impossible without an explicit fontfile where fontconfig
    # is absent, which includes a default Windows install.
    assert render.find_font(), "no font found; set REELFORGE_FONT"
    arg = render._font_arg()
    assert arg.startswith("fontfile='") and arg.endswith("':"), arg
    # Every colon in the path must be escaped or ffmpeg reads it as the
    # separator before the next filter option. A Windows path has one from
    # the drive letter; a POSIX path has none, so check the invariant rather
    # than assuming either platform.
    inner = arg[len("fontfile='"):-len("':")]
    assert ":" not in inner.replace("\\:", ""), inner


def test_still_clip_command_is_wellformed():
    cmd = render.build_still_clip_cmd(
        image="in.jpg", out="out.mp4", seconds=7, width=1080, height=1920, caption="Hi"
    )
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd and "in.jpg" in cmd and cmd[-1] == "out.mp4"
    vf = cmd[cmd.index("-vf") + 1]
    assert "zoompan" in vf and "1080:1920" in vf and "drawtext" in vf
    assert f"-t" in cmd


def test_card_clip_command_has_no_input_file():
    cmd = render.build_card_clip_cmd(
        out="out.mp4", seconds=4, width=1080, height=1920, caption="Closing", index=2
    )
    assert "lavfi" in cmd
    assert cmd[-1] == "out.mp4"


def test_concat_file_escapes_quotes():
    body = render.build_concat_list(["a b.mp4", "it's.mp4"])
    assert "'a b.mp4'" in body
    assert "it'\\''s.mp4" in body or "it\\'s.mp4" in body, body


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
