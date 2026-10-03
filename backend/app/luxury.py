"""Luxury Interiors social-video template.

This builds the five-scene copy and the brand defaults. It does not render,
call an image service, or change export dimensions. On-screen text still goes
through the existing scene title and subtitle fields.
"""
from __future__ import annotations

CATEGORY = "Luxury Interiors"
DEFAULT_BRAND = "Luxury Living Studio"
TEMPLATE_ID = "luxury_interiors"

# Words this template must not invent. The pictures are the user's.
_FORBIDDEN = (
    "marble", "budget", "₹", "$", "quote", "client", "completed project",
    "square foot", "warranty",
)

SCENE_ROLES = (
    "Opening hook",
    "Interior inspiration",
    "Design details",
    "Practical idea",
    "Branded close",
)

IMAGE_FORMAT = "Portrait 9:16, preferably 1080×1920 or higher."
IMAGE_INSTRUCTION = (
    "Copy each prompt and generate its image using your preferred image-generation tool. "
    "Download the generated image and upload it into the matching scene."
)


def _clip(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    return text[:limit].strip()


def _topic(value: str) -> str:
    return _clip(value, 160) or "a calm luxury interior"


def _brand(value: str | None) -> str:
    return _clip(value or "", 80) or DEFAULT_BRAND


def _language(value: str | None) -> str:
    return "te" if (value or "").strip().lower() == "te" else "en"


def _english(topic: str, brand: str) -> dict:
    return {
        "hook": _clip(topic, 80),
        "cta": f"{brand}. Save this idea for later.",
        "scenes": [
            {
                "role": SCENE_ROLES[0],
                "title": _clip(topic, 42),
                "subtitle": "A quieter kind of luxury",
                "narration": (
                    f"{topic}. This is an idea for the pictures you choose. "
                    "It is not a description of a finished project."
                ),
            },
            {
                "role": SCENE_ROLES[1],
                "title": "The room",
                "subtitle": "Held long enough to feel",
                "narration": (
                    "Let the interior sit on screen. Nothing here names a material, "
                    "a price, or a completed job."
                ),
            },
            {
                "role": SCENE_ROLES[2],
                "title": "The detail",
                "subtitle": "Small choices, calm result",
                "narration": "A closer look at the design, without specifying products.",
            },
            {
                "role": SCENE_ROLES[3],
                "title": "One idea",
                "subtitle": "Leave room for light",
                "narration": "A practical idea: keep the room uncluttered and leave space for light.",
            },
            {
                "role": SCENE_ROLES[4],
                "title": _clip(brand, 42),
                "subtitle": "Save this for later",
                "narration": f"{brand}. Keep this idea for the next interior you plan.",
            },
        ],
    }


def _telugu(topic: str, brand: str) -> dict:
    return {
        "hook": _clip(topic, 80),
        "cta": f"{brand}. ఈ ఆలోచనను తర్వాత కోసం ఉంచుకోండి.",
        "scenes": [
            {
                "role": SCENE_ROLES[0],
                "title": _clip(topic, 42),
                "subtitle": "ఒక ప్రశాంతమైన లగ్జరీ",
                "narration": (
                    f"{topic}. ఇది మీరు ఎంచుకున్న చిత్రాల కోసం ఒక ఆలోచన. "
                    "పూర్తి చేసిన పని వివరణ కాదు."
                ),
            },
            {
                "role": SCENE_ROLES[1],
                "title": "గది",
                "subtitle": "నెమ్మదిగా చూడండి",
                "narration": "ఇంటీరియర్ ప్రేరణ కోసం ఈ దృశ్యం. వస్తువులు లేదా ధరలు ఇక్కడ లేవు.",
            },
            {
                "role": SCENE_ROLES[2],
                "title": "వివరం",
                "subtitle": "చిన్న ఎంపికలు",
                "narration": "డిజైన్ వివరాన్ని దగ్గరగా చూడండి. ఇది ఒక నిర్దిష్ట వస్తువు హామీ కాదు.",
            },
            {
                "role": SCENE_ROLES[3],
                "title": "ఒక ఆలోచన",
                "subtitle": "కాంతికి చోటు ఇవ్వండి",
                "narration": "ఆచరణ ఆలోచన: గదిని సరళంగా ఉంచి కాంతికి ఖాళీ స్థలం ఇవ్వండి.",
            },
            {
                "role": SCENE_ROLES[4],
                "title": _clip(brand, 42),
                "subtitle": "తర్వాత కోసం దాచుకోండి",
                "narration": f"{brand}. తదుపరి ఇంటీరియర్ కోసం ఈ ఆలోచనను ఉంచుకోండి.",
            },
        ],
    }


def _caption(block: dict, brand: str, topic: str) -> str:
    return _clip(
        f"{block['hook']} {block['cta']} {brand}. {topic}.",
        500,
    )


def _description(block: dict, brand: str) -> str:
    lines = [block["hook"], ""]
    for scene in block["scenes"]:
        lines.append(f"{scene['role']}: {scene['narration']}")
    lines.append("")
    lines.append(block["cta"])
    lines.append(brand)
    return _clip("\n".join(lines), 1200)


def build_script(topic: str, brand_name: str | None = None, language: str | None = None) -> dict:
    """Editable copy in English and Telugu. The topic is the only specific claim."""
    subject = _topic(topic)
    brand = _brand(brand_name)
    chosen = _language(language)
    english = _english(subject, brand)
    telugu = _telugu(subject, brand)
    active = telugu if chosen == "te" else english
    script = {
        "template": TEMPLATE_ID,
        "topic": subject,
        "language": chosen,
        "brand_name": brand,
        "english": english,
        "telugu": telugu,
        "instagram_caption": _caption(active, brand, subject),
        "youtube_description": _description(active, brand),
        "hashtags": [
            "#LuxuryInteriors",
            "#InteriorInspiration",
            "#HomeDesign",
            "#LuxuryLiving",
            "#InteriorIdeas",
        ],
        "note": (
            "These lines are ideas for the pictures you supply. "
            "They do not describe materials, prices, or a finished project."
        ),
        "image_prompts": build_image_prompts(subject, chosen),
    }
    return script


_EN_CAMERAS = (
    "wide eye-level view",
    "slightly low angle, calm and still",
    "gentle side angle in warm evening light",
)
_EN_STYLE = (
    "Photorealistic luxury interior photograph, vertical 9:16. "
    "Keep this set consistent: warm champagne and beige tones, soft navy shadows, "
    "natural light, uncluttered, no people, no text, no logo, no watermark, no caption."
)
_EN_PURPOSES = (
    "Opening hook for {topic}: an impressive luxury home interior. {camera}. One view that establishes the home.",
    "Interior inspiration for {topic}: a beautiful living room, kitchen, or bedroom. {camera}. Show the whole room, calm and open.",
    "Design details for {topic}: lighting, furniture, marble, wood, or wall design. {camera}. One material detail, with no product name.",
    "A practical interior idea for {topic}. {camera}. Show how open space and light make the room easier to live in.",
    "Closing scene for {topic}: a beautiful final interior shot for the brand ending. {camera}. Keep the lower area visually calm.",
)
_TE_CAMERAS = (
    "కంటి స్థాయి విశాల దృశ్యం",
    "కొంచెం దిగువ కోణం, ప్రశాంతంగా",
    "వెచ్చని సాయంత్రం వెలుతురులో పక్క కోణం",
)
_TE_STYLE = (
    "ఫోటోరియలిస్టిక్ లగ్జరీ ఇంటీరియర్, నిలువు 9:16. "
    "అన్ని ఐదు చిత్రాలు ఒకే శైలి: వెచ్చని బెజ్ టోన్లు, మృదువైన నీలి నీడలు, "
    "ప్రజలు లేరు, అక్షరాలు లేవు, లోగో లేదు, వాటర్‌మార్క్ లేదు, క్యాప్షన్ లేదు."
)
_TE_PURPOSES = (
    "{topic} తెరిచే దృశ్యం. {camera}. ఆకట్టుకునే లగ్జరీ ఇల్లు లేదా గదిని ఒకే చూపులో చూపించు.",
    "{topic} ఇంటీరియర్ ప్రేరణ. {camera}. అందమైన లివింగ్ రూమ్, కిచెన్ లేదా బెడ్‌రూమ్.",
    "{topic} డిజైన్ వివరాలు. {camera}. లైటింగ్, ఫర్నీచర్, మార్బుల్, కలప లేదా గోడ డిజైన్. వస్తువు పేరు లేదు.",
    "{topic} ఆచరణ ఆలోచన. {camera}. కాంతి మరియు ఖాళీ స్థలం గదిని సులభంగా ఉంచేలా చూపించు.",
    "{topic} ముగింపు దృశ్యం. {camera}. బ్రాండ్ ముగింపుకు తగిన అందమైన చివరి ఇంటీరియర్. దిగువ భాగం ప్రశాంతంగా ఉంచు.",
)


def _variant(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value) % 3
    except ValueError:
        return 0


def image_prompt(topic: str, language: str | None, index: int, variant: int = 0) -> str:
    """A still prompt for an external image tool. No network call."""
    subject = _topic(topic)
    chosen = _language(language)
    slot = index if 0 <= index < len(SCENE_ROLES) else 0
    camera_index = _variant(variant)
    if chosen == "te":
        body = _TE_PURPOSES[slot].format(topic=subject, camera=_TE_CAMERAS[camera_index])
        return _clip(f"{_TE_STYLE} {body}", 800)
    body = _EN_PURPOSES[slot].format(topic=subject, camera=_EN_CAMERAS[camera_index])
    return _clip(f"{_EN_STYLE} {body}", 800)


def build_image_prompts(topic: str, language: str | None = None) -> dict:
    chosen = _language(language)
    return {
        "format": IMAGE_FORMAT,
        "instruction": IMAGE_INSTRUCTION,
        "scenes": [
            {
                "role": SCENE_ROLES[index],
                "prompt": image_prompt(topic, chosen, index, 0),
                "variant": 0,
            }
            for index in range(len(SCENE_ROLES))
        ],
    }


def image_prompts_ready(script: dict) -> bool:
    pack = script.get("image_prompts")
    if not isinstance(pack, dict):
        return False
    scenes = pack.get("scenes")
    return isinstance(scenes, list) and len(scenes) == len(SCENE_ROLES)


def replace_image_prompt(script: dict, index: int) -> dict:
    """Rewrite one still prompt. The other four stay as saved."""
    updated = dict(script)
    if not image_prompts_ready(updated):
        pack = build_image_prompts(str(updated.get("topic") or ""), updated.get("language"))
    else:
        pack = dict(updated["image_prompts"])
    scenes = [dict(scene) for scene in pack["scenes"]]
    slot = index if 0 <= index < len(SCENE_ROLES) else 0
    nxt = (_variant(scenes[slot].get("variant")) + 1) % 3
    scenes[slot] = {
        "role": SCENE_ROLES[slot],
        "prompt": image_prompt(str(updated.get("topic") or ""), updated.get("language"), slot, nxt),
        "variant": nxt,
    }
    updated["image_prompts"] = {
        "format": IMAGE_FORMAT,
        "instruction": IMAGE_INSTRUCTION,
        "scenes": scenes,
    }
    return updated


def on_screen_lines(script: dict) -> list[tuple[str, str]]:
    """Title and subtitle for each scene, in the script's selected language."""
    chosen = _language(script.get("language"))
    block = script.get("telugu") if chosen == "te" else script.get("english")
    scenes = block.get("scenes") if isinstance(block, dict) else None
    if not isinstance(scenes, list):
        return []
    lines: list[tuple[str, str]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        lines.append((
            _clip(str(scene.get("title") or ""), 80),
            _clip(str(scene.get("subtitle") or ""), 120),
        ))
    return lines


def missing_image_message(scenes: list) -> str | None:
    """Explain which Luxury Interiors scenes still have no photo."""
    missing = [
        index + 1
        for index, scene in enumerate(scenes)
        if not (scene or {}).get("asset_id")
    ]
    if not missing:
        return None
    if len(missing) == 1:
        listed = f"Scene {missing[0]}"
        verb = "needs"
    else:
        listed = "Scenes " + ", ".join(str(number) for number in missing)
        verb = "need"
    return (
        f"{listed} still {verb} an image. "
        "Upload or assign a photo to each scene before rendering."
    )


def contains_invented_claim(script: dict) -> str | None:
    """Return the first forbidden phrase in on-screen copy.

    Image prompts may name materials such as marble because they describe a
    picture to create, not a claim about a finished project.
    """
    visible = {
        key: value for key, value in script.items() if key != "image_prompts"
    }
    blob = _walk(visible).lower()
    for phrase in _FORBIDDEN:
        if phrase.lower() in blob:
            return phrase
    return None


def _walk(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_walk(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_walk(item) for item in value)
    return ""


def _clean_block(block: object) -> dict | None:
    if not isinstance(block, dict):
        return None
    scenes_in = block.get("scenes")
    scenes: list[dict] = []
    if isinstance(scenes_in, list):
        for index, scene in enumerate(scenes_in[:5]):
            if not isinstance(scene, dict):
                continue
            role = SCENE_ROLES[index] if index < len(SCENE_ROLES) else _clip(str(scene.get("role") or "Scene"), 40)
            scenes.append({
                "role": role,
                "title": _clip(str(scene.get("title") or ""), 80),
                "subtitle": _clip(str(scene.get("subtitle") or ""), 120),
                "narration": _clip(str(scene.get("narration") or ""), 400),
            })
    return {
        "hook": _clip(str(block.get("hook") or ""), 120),
        "cta": _clip(str(block.get("cta") or ""), 160),
        "scenes": scenes,
    }


def merge_edits(current: dict, fields: dict) -> dict:
    """Keep a customer's edits. Does not call a model and does not add claims."""
    script = dict(current)
    script["template"] = TEMPLATE_ID
    if fields.get("topic"):
        script["topic"] = _topic(str(fields["topic"]))
    if fields.get("brand_name"):
        script["brand_name"] = _brand(str(fields["brand_name"]))
    if fields.get("language"):
        script["language"] = _language(str(fields["language"]))
    else:
        script["language"] = _language(str(script.get("language") or "en"))
    for key, limit in (("instagram_caption", 500), ("youtube_description", 1200), ("note", 240)):
        if fields.get(key) is not None:
            script[key] = _clip(str(fields[key]), limit)
    if isinstance(fields.get("hashtags"), list):
        tags: list[str] = []
        for tag in fields["hashtags"][:12]:
            text = _clip(str(tag), 40)
            if not text:
                continue
            if not text.startswith("#"):
                text = "#" + text.replace(" ", "")
            tags.append(text)
        script["hashtags"] = tags
    for name in ("english", "telugu"):
        cleaned = _clean_block(fields.get(name))
        if cleaned is not None:
            script[name] = cleaned
    cleaned_prompts = _clean_image_prompts(
        fields.get("image_prompts"), script["topic"], script["language"],
    )
    if cleaned_prompts is not None:
        script["image_prompts"] = cleaned_prompts
    script["brand_name"] = _brand(str(script.get("brand_name") or ""))
    return script


def _clean_image_prompts(value: object, topic: str, language: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    scenes_in = value.get("scenes")
    if not isinstance(scenes_in, list):
        return None
    scenes: list[dict] = []
    for index in range(len(SCENE_ROLES)):
        scene = scenes_in[index] if index < len(scenes_in) and isinstance(scenes_in[index], dict) else {}
        variant = _variant(scene.get("variant"))
        prompt = _clip(str(scene.get("prompt") or ""), 800)
        if not prompt:
            prompt = image_prompt(topic, language, index, variant)
        scenes.append({
            "role": SCENE_ROLES[index],
            "prompt": prompt,
            "variant": variant,
        })
    return {
        "format": IMAGE_FORMAT,
        "instruction": IMAGE_INSTRUCTION,
        "scenes": scenes,
    }


def image_generation_status() -> dict:
    """No image provider is wired. Do not turn a paid API on from here."""
    return {
        "available": False,
        "message": IMAGE_INSTRUCTION,
    }
