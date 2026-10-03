"""Storyboard planner.

The template planner builds a beat sheet whose durations sum to exactly
`duration`. `plan` is the entry point project creation calls. It can later
select a vision-capable model planner; that planner is not connected, and
any failure falls back to the template.

The generation adapter seam lives in render.py; this module only decides
*what* each scene should show.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from . import config, luxury, repo

log = logging.getLogger("reelforge.storyboard")

MIN_SCENE_SECONDS = 2
MIN_SCENES = 3
MAX_SCENES = 8

# Per-category beat sheets: (title, prompt template, relative weight).
# `{idea}` is substituted with the user's idea text.
BEAT_SHEETS: dict[str, list[tuple[str, str, float]]] = {
    "Cinematic": [
        ("Opening", "Wide establishing shot that introduces the world of: {idea}", 1.1),
        ("Main moment", "The central subject of {idea}, slow push-in, shallow depth of field", 1.3),
        ("Detail", "Macro detail that carries the texture and mood of {idea}", 0.9),
        ("Story beat", "A turn in the story — motion, light change, rising emotion", 1.0),
        ("Closing", "Memorable final frame with negative space for a title card", 0.9),
    ],
    "Product": [
        ("Hero shot", "Clean hero product shot on a seamless backdrop: {idea}", 1.0),
        ("In use", "The product being used naturally in its real context", 1.2),
        ("Feature detail", "Tight detail on the single most compelling feature", 1.0),
        ("Proof", "The result or benefit the buyer actually cares about", 1.0),
        ("Call to action", "Product centred with clear space for price and CTA", 0.8),
    ],
    "Business": [
        ("The problem", "Visual statement of the problem faced by the audience of: {idea}", 1.0),
        ("The team", "People at work, warm and credible, natural light", 1.1),
        ("The solution", "How the offering resolves the problem, shown not told", 1.3),
        ("Proof", "A concrete result, metric or client moment", 0.9),
        ("Closing", "Logo-ready closing frame with breathing room", 0.7),
    ],
    "Real Estate": [
        ("Approach", "Exterior approach to the property, golden hour: {idea}", 1.0),
        ("Entry", "Walk-through entry revealing the main living space", 1.2),
        ("Feature room", "The single strongest room, wide and bright", 1.2),
        ("Detail", "Material and finish detail that signals quality", 0.8),
        ("Outdoor", "Outdoor space, view or aspect, ending on a held frame", 1.0),
    ],
    "Personal": [
        ("Introduction", "Portrait introducing the subject of: {idea}", 1.0),
        ("In their element", "The subject doing the thing they are known for", 1.3),
        ("Detail", "Hands, tools or texture that tells the personal story", 0.9),
        ("Turn", "A candid, human moment that builds connection", 1.0),
        ("Closing", "Direct-to-camera closing frame, confident and warm", 0.8),
    ],
    "Social Media": [
        ("Hook", "Scroll-stopping first frame for: {idea}", 0.7),
        ("Setup", "Fast context so the viewer knows what they are watching", 0.9),
        ("Payoff", "The satisfying moment the hook promised", 1.2),
        ("Bonus", "One more beat to hold the viewer past the midpoint", 0.9),
        ("Loop", "Closing frame that visually loops back to the hook", 0.7),
    ],
    "Event": [
        ("Arrival", "Guests arriving, atmosphere building: {idea}", 1.0),
        ("The moment", "The centrepiece moment of the event, full energy", 1.3),
        ("Faces", "Reaction shots — laughter, applause, connection", 1.0),
        ("Detail", "Styling, food or decor detail that sets the tone", 0.8),
        ("Farewell", "Warm closing frame as the night winds down", 0.9),
    ],
    "Creative": [
        ("Blank", "An empty, expectant frame before anything happens: {idea}", 0.8),
        ("Emergence", "Form and colour beginning to assemble", 1.1),
        ("Peak", "The fullest, most saturated expression of the concept", 1.3),
        ("Break", "A deliberate visual contradiction or rupture", 0.9),
        ("Resolve", "Everything settles into a composed final image", 0.9),
    ],
    # Five beats, kept at five scenes when the duration can hold them.
    # The existing motion, grade, and export presets are unchanged.
    luxury.CATEGORY: [
        ("Opening hook", "Slow opening frame for {idea}, warm neutral light", 1.0),
        ("Interior inspiration", "A calm luxury interior inspired by {idea}", 1.2),
        ("Design detail", "A close view of a design detail in {idea}", 1.1),
        ("Practical idea", "An uncluttered view that explains one design idea for {idea}", 1.1),
        ("Branded close", "A quiet closing frame for {idea}, space for a short title", 0.9),
    ],
}

DEFAULT_SHEET = BEAT_SHEETS["Cinematic"]

ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


def resolution_for(aspect_ratio: str) -> tuple[int, int]:
    return ASPECT_SIZES.get(aspect_ratio, ASPECT_SIZES["9:16"])


def pick_scene_count(duration: int) -> int:
    """How many cuts fit in `duration` seconds.

    Roughly one scene per 6 seconds. This is the pacing dial: lower the
    divisor for faster, more TikTok-like cutting, raise it for longer
    cinematic holds.

    Capped at `duration` so we never plan more scenes than there are whole
    seconds to give them — a zero-second clip fails the render outright.
    """
    target = max(MIN_SCENES, min(MAX_SCENES, round(duration / 6) or MIN_SCENES))
    return max(1, min(target, duration))


def split_duration(total: int, weights: list[float]) -> list[int]:
    """Split `total` seconds across `weights`, summing to exactly `total`.

    Every scene gets at least MIN_SCENE_SECONDS where there is room, and
    never less than 1 second. Rounding remainder is handed to the heaviest
    scenes first, so the beats that matter absorb the slack instead of the
    closing card.
    """
    n = len(weights)
    if n == 0:
        return []
    if total < n:
        raise ValueError(f"cannot split {total}s across {n} scenes without a 0s clip")
    floor_total = MIN_SCENE_SECONDS * n
    if total <= floor_total:
        # Not enough room to weight anything; spread as evenly as possible.
        base, extra = divmod(total, n)
        return [base + (1 if i < extra else 0) for i in range(n)]

    spare = total - floor_total
    weight_sum = sum(weights) or float(n)
    raw = [spare * w / weight_sum for w in weights]
    out = [MIN_SCENE_SECONDS + int(r) for r in raw]

    remainder = total - sum(out)
    # Give leftover seconds to the largest fractional parts, heaviest weight wins ties.
    order = sorted(range(n), key=lambda i: (raw[i] % 1, weights[i]), reverse=True)
    for i in range(remainder):
        out[order[i % n]] += 1
    return out


def beats_for_count(sheet: list[tuple[str, str, float]], count: int) -> list[tuple[str, str, float]]:
    """Stretch or trim a beat sheet to `count` beats.

    Extra beats repeat the middle of the sheet, which is where the substance
    is. Trimming keeps the first and last beat so the reel still opens and
    closes. A count of one keeps the opening beat.
    """
    if count <= 0:
        return []
    if count <= len(sheet):
        keep = [0] + [
            1 + round(i * (len(sheet) - 2) / max(count - 2, 1))
            for i in range(count - 2)
        ] + [len(sheet) - 1]
        return [sheet[i] for i in keep[:count]]
    beats = list(sheet)
    middle = sheet[1:-1] or sheet
    i = 0
    while len(beats) < count:
        title, prompt, weight = middle[i % len(middle)]
        beats.insert(-1, (f"{title} {2 + i // len(middle)}", prompt, weight))
        i += 1
    return beats


def _scene_rows(idea: str, beats: list[tuple[str, str, float]], durations: list[int]) -> list[dict]:
    clean_idea = (idea or "your idea").strip()
    spoken = (idea or "").strip()
    return [
        {
            "position": i,
            "title": title,
            "prompt": prompt.format(idea=clean_idea),
            "duration": secs,
            # The customer's own words, once, on the opening scene. Later
            # scenes stay blank unless the customer writes a caption.
            "caption": spoken if i == 0 and spoken else None,
        }
        for i, ((title, prompt, _), secs) in enumerate(zip(beats, durations))
    ]


def plan_scenes(idea: str, category: str, duration: int) -> list[dict]:
    sheet = BEAT_SHEETS.get(category, DEFAULT_SHEET)
    # The luxury template stays five scenes when each scene can be at least
    # two seconds. A longer reel lengthens those scenes instead of adding cuts.
    if (
        category == luxury.CATEGORY
        and duration >= len(sheet) * MIN_SCENE_SECONDS
    ):
        count = len(sheet)
    else:
        count = pick_scene_count(duration)
    beats = beats_for_count(sheet, count)
    durations = split_duration(duration, [b[2] for b in beats])
    return _scene_rows(idea, beats, durations)


def scenes_for_image_count(idea: str, category: str, duration: int, count: int) -> list[dict]:
    """One beat per uploaded image. `count` is clamped to 1–8.

    Durations come from the same splitter the template uses, so they sum to
    `duration`. This does not use pick_scene_count: a single photo is one
    scene, not the three-scene minimum used for a title-card reel.
    """
    count = max(1, min(MAX_SCENES, int(count)))
    sheet = BEAT_SHEETS.get(category, DEFAULT_SHEET)
    beats = beats_for_count(sheet, count)
    durations = split_duration(duration, [b[2] for b in beats])
    return _scene_rows(idea, beats, durations)


@dataclass(frozen=True)
class StoryboardImage:
    """One uploaded image the model planner may look at later.

    `reference` is a URL or storage path. Raw image bytes are never placed
    on this object.
    """

    asset_id: str
    filename: str
    reference: str
    position: int


@dataclass(frozen=True)
class StoryboardBrief:
    """What a model planner receives. Images may be empty at project creation."""

    idea: str
    category: str
    duration: int
    input_type: str
    images: tuple[StoryboardImage, ...]


@dataclass
class PlannedStoryboard:
    """A storyboard plus the model's desired image assignment.

    `scenes` matches the existing scene contract. `assignments` maps a scene
    position to an asset id and is not written to the database by the planner.
    """

    scenes: list[dict]
    assignments: dict[int, str]


class StoryboardModelError(Exception):
    """The model planner could not produce a storyboard. Customers never see this."""


class ModelPlanner(Protocol):
    """What a future vision-capable planner must implement."""

    def plan(self, brief: StoryboardBrief) -> dict:
        """Return a raw storyboard payload. Do not raise customer-facing errors."""


class UnconnectedModelPlanner:
    """Stand-in for a future vision-capable planner. It never calls a service."""

    def plan(self, brief: StoryboardBrief) -> dict:
        del brief
        raise StoryboardModelError("storyboard model is not connected")


def _provider_name(value: str | None) -> str:
    chosen = (value or "").strip().lower()
    if chosen == "model":
        return "model"
    return "template"


def _images_from_names(names: list[str] | None) -> tuple[StoryboardImage, ...]:
    images: list[StoryboardImage] = []
    for index, name in enumerate(names or []):
        filename = str(name).strip()
        if not filename:
            continue
        images.append(
            StoryboardImage(
                asset_id="",
                filename=filename,
                reference=filename,
                position=index,
            )
        )
    return tuple(images)


def _template_storyboard(idea: str, category: str, duration: int) -> PlannedStoryboard:
    return PlannedStoryboard(
        scenes=plan_scenes(idea, category, duration),
        assignments={},
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _scene_duration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("duration must be a positive integer")
    return value


def _scene_position(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("position must be a non-negative integer")
    return value


def validate_model_output(raw: object, brief: StoryboardBrief) -> PlannedStoryboard:
    """Check a model payload and return the existing scene contract.

    Raises ValueError when the payload cannot be used. Callers fall back to
    the template planner instead of showing this to the customer.
    """
    if not isinstance(raw, dict):
        raise ValueError("model output must be an object")
    scenes = raw.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("model output must include scenes")
    if not MIN_SCENES <= len(scenes) <= MAX_SCENES or len(scenes) > brief.duration:
        raise ValueError("scene count is not usable for this duration")

    allowed = {image.asset_id for image in brief.images if image.asset_id}
    planned: list[dict] = []
    assignments: dict[int, str] = {}
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError("each scene must be an object")
        position = _scene_position(scene.get("position"))
        if position != index:
            raise ValueError("scene positions must be sequential from zero")
        caption = scene.get("caption", None)
        if caption is not None and not isinstance(caption, str):
            raise ValueError("caption must be text or null")
        asset_id = scene.get("asset_id", None)
        if asset_id is not None:
            if not isinstance(asset_id, str) or asset_id not in allowed:
                raise ValueError("image reference is not one of the supplied images")
            assignments[position] = asset_id
        planned.append(
            {
                "position": position,
                "title": _required_text(scene.get("title"), "title"),
                "prompt": _required_text(scene.get("prompt"), "prompt"),
                "duration": _scene_duration(scene.get("duration")),
                "caption": caption,
            }
        )

    if sum(scene["duration"] for scene in planned) != brief.duration:
        raise ValueError("scene durations must add up to the requested duration")
    return PlannedStoryboard(scenes=planned, assignments=assignments)


def plan_storyboard(
    idea: str,
    category: str,
    duration: int,
    input_type: str,
    image_names: list[str] | None = None,
    images: list[StoryboardImage] | None = None,
    model: ModelPlanner | None = None,
) -> PlannedStoryboard:
    """Select the configured planner. A model failure returns the template.

    Pass `images` once uploads exist. `image_names` covers the earlier call,
    which has filenames only. Neither path sends image bytes.
    """
    if images is None:
        prepared = _images_from_names(image_names)
    else:
        prepared = tuple(images)
    if _provider_name(config.STORYBOARD_PROVIDER) != "model":
        return _template_storyboard(idea, category, duration)

    brief = StoryboardBrief(
        idea=idea,
        category=category,
        duration=duration,
        input_type=input_type,
        images=prepared,
    )
    planner = model or UnconnectedModelPlanner()
    try:
        return validate_model_output(planner.plan(brief), brief)
    except Exception as exc:
        log.warning(
            "storyboard model failed (%s); using the template planner",
            type(exc).__name__,
        )
        return _template_storyboard(idea, category, duration)


def images_from_stored_assets(assets: list[dict], reference_for) -> list[StoryboardImage]:
    """Turn stored image rows into planner images, in upload order.

    `reference_for` is the storage URL helper. It receives a storage key and
    must not be given image bytes. Audio rows are skipped.
    """
    images: list[StoryboardImage] = []
    for asset in assets:
        if asset.get("kind") != "image":
            continue
        asset_id = str(asset.get("id") or "").strip()
        storage_key = str(asset.get("storage_key") or "").strip()
        if not asset_id or not storage_key:
            continue
        reference = reference_for(storage_key) or storage_key
        filename = str(asset.get("filename") or "").strip() or asset_id
        images.append(
            StoryboardImage(
                asset_id=asset_id,
                filename=filename,
                reference=str(reference),
                position=len(images),
            )
        )
    return images


def _scene_layout_matches(existing: list[dict], planned_scenes: list[dict]) -> bool:
    if len(existing) != len(planned_scenes):
        return False
    return [scene["position"] for scene in existing] == [
        scene["position"] for scene in planned_scenes
    ]


def _drop_clip(stale: list[str], conn, project_id: str, scene_id: str) -> None:
    key = repo.clear_clip(conn, project_id, scene_id)
    if key and key not in stale:
        stale.append(key)


def _plan_fits_project(conn, project: dict, existing: list[dict], planned: PlannedStoryboard) -> bool:
    """A plan is applied entirely or not at all."""
    if not _scene_layout_matches(existing, planned.scenes):
        return False
    durations = [scene["duration"] for scene in planned.scenes]
    if sum(durations) != int(project["duration"]):
        return False
    if not all(
        isinstance(part, int) and not isinstance(part, bool) and part > 0
        for part in durations
    ):
        return False
    positions = {scene["position"] for scene in existing}
    for position, asset_id in planned.assignments.items():
        if position not in positions or not isinstance(asset_id, str):
            return False
        asset = repo.get_asset(conn, project["id"], asset_id)
        if asset is None or asset["kind"] != "image":
            return False
    return True


def apply_planned_storyboard(conn, project: dict, planned: PlannedStoryboard) -> list[str]:
    """Write a plan onto the scenes that already exist.

    Scene rows stay put, so ids from the create response remain valid while
    later photos upload. When the plan names image assignments, those links
    are the final ones. An empty assignment map leaves the upload-order links
    already stored. Returns clip cache keys that are no longer referenced.
    """
    project_id = project["id"]
    existing = repo.list_scenes(conn, project_id)
    if not _plan_fits_project(conn, project, existing, planned):
        log.warning(
            "storyboard plan does not fit the project; keeping the current storyboard"
        )
        return []

    by_position = {scene["position"]: scene for scene in existing}
    stale: list[str] = []
    changed = False
    for planned_scene in planned.scenes:
        current = by_position[planned_scene["position"]]
        fields = {
            key: planned_scene[key]
            for key in ("title", "prompt", "duration", "caption")
            if planned_scene[key] != current[key]
        }
        if not fields:
            continue
        repo.update_scene(conn, project_id, current["id"], fields)
        _drop_clip(stale, conn, project_id, current["id"])
        changed = True
    if changed:
        repo.sync_project_duration(conn, project_id)

    for position, asset_id in planned.assignments.items():
        scene = by_position[position]
        if scene.get("asset_id") == asset_id:
            continue
        repo.attach_asset_to_scene(conn, project_id, scene["id"], asset_id)
        _drop_clip(stale, conn, project_id, scene["id"])
        changed = True

    if changed:
        repo.invalidate_render(conn, project_id)
    return stale


def _fill_empty_scenes(conn, project: dict) -> list[str]:
    """Attach unused photos to scenes that have none, without rewriting the reel.

    Used by the luxury template so an upload does not replace the five scenes
    or their text. Extra photos stay in the project library.
    """
    images = [
        asset for asset in repo.list_assets(conn, project["id"])
        if asset["kind"] == "image"
    ]
    used = {
        scene.get("asset_id")
        for scene in repo.list_scenes(conn, project["id"])
        if scene.get("asset_id")
    }
    waiting = [asset for asset in images if asset["id"] not in used]
    stale: list[str] = []
    changed = False
    for scene in repo.list_scenes(conn, project["id"]):
        if scene.get("asset_id") or not waiting:
            continue
        asset = waiting.pop(0)
        repo.attach_asset_to_scene(conn, project["id"], scene["id"], asset["id"])
        _drop_clip(stale, conn, project["id"], scene["id"])
        changed = True
    if changed:
        repo.invalidate_render(conn, project["id"])
    return stale


def fit_image_timeline(conn, project: dict) -> list[str] | None:
    """Replace the storyboard with one scene per uploaded image, in upload order.

    Returns clip keys that are no longer referenced, or None when there is
    nothing to fit (no images, or the duration cannot be split without a
    zero-second clip). A caption the customer already wrote on the opening
    scene is kept when the scene count does not change.
    """
    if project.get("category") == luxury.CATEGORY:
        return _fill_empty_scenes(conn, project)
    images = [
        asset for asset in repo.list_assets(conn, project["id"])
        if asset["kind"] == "image"
    ][:MAX_SCENES]
    if not images:
        return None
    existing = repo.list_scenes(conn, project["id"])
    try:
        planned = scenes_for_image_count(
            project.get("idea") or "",
            project.get("category") or "",
            int(project["duration"]),
            len(images),
        )
    except ValueError:
        log.warning(
            "cannot give %d images their own scenes inside %ss; leaving the storyboard",
            len(images), project.get("duration"),
        )
        return None
    if (
        len(existing) == len(planned)
        and (existing[0].get("caption") or "").strip()
    ):
        planned[0]["caption"] = existing[0]["caption"]
    return repo.replace_scenes(conn, project["id"], [
        {**scene, "asset_id": images[scene["position"]]["id"]}
        for scene in planned
    ])


def replan_uploaded_project(conn, project: dict, reference_for, model: ModelPlanner | None = None) -> list[str]:
    """Plan again from the images stored on this project, then apply the result.

    A planner failure keeps the storyboard already saved at project creation.
    """
    if project.get("category") == luxury.CATEGORY:
        # Keep the five-scene template. Photos are attached by fit_image_timeline.
        return []
    try:
        images = images_from_stored_assets(repo.list_assets(conn, project["id"]), reference_for)
        planned = plan_storyboard(
            project.get("idea") or "",
            project.get("category") or "",
            int(project["duration"]),
            project.get("input_type") or "",
            images=images,
            model=model,
        )
        return apply_planned_storyboard(conn, project, planned)
    except Exception as exc:
        log.warning(
            "post-upload storyboard plan failed (%s); keeping the current storyboard",
            type(exc).__name__,
        )
        return []


def plan(
    idea: str,
    category: str,
    duration: int,
    input_type: str,
    image_names: list[str],
) -> list[dict]:
    """Plan a storyboard from the customer's project.

    Returns the same scene dicts the template planner always has. When a model
    planner is configured, invalid or failed output falls back to that template.
    Image assignment stays off this list so the existing scene rows are unchanged.
    """
    return plan_storyboard(
        idea, category, duration, input_type, image_names
    ).scenes


def reprompt_scene(idea: str, category: str, title: str, attempt: int) -> str:
    """Produce a different prompt for the same beat, for scene regeneration."""
    sheet = BEAT_SHEETS.get(category, DEFAULT_SHEET)
    variations = [
        "shot on 35mm, natural light, handheld",
        "tripod-locked, symmetrical composition, soft key light",
        "slow dolly move, anamorphic flare, high contrast",
        "overhead angle, diffused light, muted palette",
        "low angle, backlit, volumetric haze",
    ]
    base = next((p for t, p, _ in sheet if t.split()[0] == title.split()[0]), sheet[0][1])
    style = variations[attempt % len(variations)]
    return f"{base.format(idea=(idea or 'your idea').strip())} — {style}"
