"""Deterministic storyboard planner.

Replaces the hardcoded 5-scene / 30-second list the starter had in both
main.py and the frontend. Scene durations are derived from the requested
total, so they always sum to exactly `duration`.

The generation adapter seam lives in render.py; this module only decides
*what* each scene should show.
"""
from __future__ import annotations

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


def plan_scenes(idea: str, category: str, duration: int) -> list[dict]:
    sheet = BEAT_SHEETS.get(category, DEFAULT_SHEET)
    count = pick_scene_count(duration)

    # Stretch or trim the beat sheet to `count` beats. Extra beats repeat the
    # middle of the sheet, which is where the substance is; trimming always
    # keeps the first and last beat so the reel still opens and closes.
    if count <= len(sheet):
        keep = [0] + [
            1 + round(i * (len(sheet) - 2) / max(count - 2, 1))
            for i in range(count - 2)
        ] + [len(sheet) - 1]
        beats = [sheet[i] for i in keep[:count]]
    else:
        beats = list(sheet)
        middle = sheet[1:-1] or sheet
        i = 0
        while len(beats) < count:
            title, prompt, weight = middle[i % len(middle)]
            beats.insert(-1, (f"{title} {2 + i // len(middle)}", prompt, weight))
            i += 1

    durations = split_duration(duration, [b[2] for b in beats])
    clean_idea = (idea or "your idea").strip()
    return [
        {
            "position": i,
            "title": title,
            "prompt": prompt.format(idea=clean_idea),
            "duration": secs,
            "caption": None,
        }
        for i, ((title, prompt, _), secs) in enumerate(zip(beats, durations))
    ]


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
