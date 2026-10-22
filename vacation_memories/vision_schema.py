"""What the vision model is asked and how its answer is validated.

The answer is requested as structured output (a JSON schema in the `format` parameter) and checked
again here: a field that is missing or invalid becomes `None`, but the frame is never discarded. The
model judges the content only; technical quality is measured elsewhere and the two must not mix.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

CATEGORIES = ("nature", "sea", "city", "monument", "food", "animals", "indoor", "other")
MOMENT_TYPES = ("scenery", "social", "action", "detail", "transition", "other")
MAX_DESCRIPTION_WORDS = 15
# The word limit cannot be expressed in the schema, but a length limit can: it makes the model close the
# string. Without it a small model may repeat itself until `num_predict` cuts the JSON off, losing every field.
MAX_DESCRIPTION_CHARS = 160
MIN_NARRATIVE_VALUE = 1
MAX_NARRATIVE_VALUE = 5

RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "maxLength": MAX_DESCRIPTION_CHARS},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "moment_type": {"type": "string", "enum": list(MOMENT_TYPES)},
        "narrative_value": {"type": "integer", "minimum": MIN_NARRATIVE_VALUE, "maximum": MAX_NARRATIVE_VALUE},
        "people": {"type": "boolean"},
    },
    "required": ["description", "category", "moment_type", "narrative_value", "people"],
}

PROMPT = f"""You are helping to pick the best moments of a vacation for a memory video.
Look at the image and describe what it shows. Answer with a JSON object with these fields:

- description: what the image shows, at most {MAX_DESCRIPTION_WORDS} words.
- category: the subject or environment, one of: {", ".join(CATEGORIES)}.
- moment_type: the narrative role of the scene, one of: {", ".join(MOMENT_TYPES)}.
  - scenery: wide view or environment shot with no main subject (panorama, skyline, beach).
  - social: people together, posing or interacting (meals, gatherings, groups).
  - action: clearly visible movement or activity (swimming, sports, walking, vehicle in motion).
  - detail: close-up of an object, food, sign, texture.
  - transition: linking material (road, window, travel, waiting, arrivals and departures).
  - other: anything that does not fit the values above.
- narrative_value: an integer from {MIN_NARRATIVE_VALUE} to {MAX_NARRATIVE_VALUE}, how much the scene deserves a place in a
  memory video (originality, ability to tell a moment). {MIN_NARRATIVE_VALUE} is banal, {MAX_NARRATIVE_VALUE} is a highlight.
- people: true if people are visible, otherwise false.

Judge the content only. Do not judge sharpness, exposure or composition: the technical quality is
measured elsewhere. Do not guess identities, personal emotions or specific places that cannot be
observed with certainty in the image."""


class VisionResponseError(Exception):
    """The model answered, but not with a JSON object."""


@dataclass(frozen=True)
class VisionResult:
    """The validated answer for one frame; every field is `None` when missing or invalid."""

    description: str | None
    category: str | None
    moment_type: str | None
    narrative_value: int | None
    people: bool | None

    @classmethod
    def from_response(cls, raw: object) -> "VisionResult":
        if not isinstance(raw, dict):
            raise VisionResponseError(f"expected a JSON object, got {type(raw).__name__}")
        return cls(
            description=_description(raw.get("description")),
            category=_choice(raw.get("category"), CATEGORIES),
            moment_type=_choice(raw.get("moment_type"), MOMENT_TYPES),
            narrative_value=_narrative_value(raw.get("narrative_value")),
            people=raw.get("people") if isinstance(raw.get("people"), bool) else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)


def prompt_fingerprint(prompt: str = PROMPT, schema: dict = RESPONSE_SCHEMA) -> str:
    """Identifies the question asked; part of the cache key, so a new prompt invalidates old answers."""
    payload = json.dumps({"prompt": prompt, "schema": schema}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _description(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text.split()) <= MAX_DESCRIPTION_WORDS else None


def _choice(value: object, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _narrative_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if MIN_NARRATIVE_VALUE <= value <= MAX_NARRATIVE_VALUE else None
