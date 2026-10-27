"""Weights, thresholds, shares and durations of the selection, in one place.

These are the initial values of the specification (section 6), meant to be tuned on the first real
dataset. Any of them can be changed from the `[selection]` table of the configuration file, without
touching the code, for example:

    [selection]
    technical_threshold = 0.3
    narrative_weight = 0.7
    technical_weight = 0.3

    [selection.sections.opening]
    share = 0.08

    [selection.moment_shares]
    detail = 0.2
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace

SECTIONS = ("opening", "beginning", "development", "finale", "closing")
BODY_SECTIONS = ("beginning", "development", "finale")
MOMENT_TYPES = ("scenery", "social", "action", "detail", "transition", "other")


@dataclass(frozen=True)
class SectionShare:
    """Starting share of `--duration` and the range it may move in when adapting to the material."""

    share: float
    min: float
    max: float


def _default_sections() -> dict[str, SectionShare]:
    return {
        "opening": SectionShare(0.10, 0.05, 0.15),
        "beginning": SectionShare(0.20, 0.15, 0.25),
        "development": SectionShare(0.45, 0.35, 0.55),
        "finale": SectionShare(0.20, 0.15, 0.25),
        "closing": SectionShare(0.05, 0.03, 0.08),
    }


def _default_moment_shares() -> dict[str, float]:
    """Soft shares of the edit per `moment_type`: going beyond them lowers the score, it does not forbid."""
    return {"scenery": 0.25, "social": 0.30, "action": 0.25, "detail": 0.15, "transition": 0.10, "other": 0.15}


@dataclass(frozen=True)
class SelectionConfig:
    # Score: technical threshold, then a weighted sum with more weight on the narrative.
    technical_threshold: float = 0.35  # below it (blurred, black, badly exposed) a candidate is discarded
    max_black_fraction: float = 0.5  # a clip segment that is black for more than this is discarded
    narrative_weight: float = 0.65
    technical_weight: float = 0.35

    # Components of `narrative_score`; without the AI only uniqueness, motion and temporal are used.
    ai_value_weight: float = 0.45
    uniqueness_weight: float = 0.15
    variety_weight: float = 0.15
    motion_weight: float = 0.10
    temporal_weight: float = 0.15
    full_motion: float = 0.08  # median luma change between frames that counts as fully dynamic
    photo_motion: float = 0.3  # a photo has no motion: this neutral value stands for it
    temporal_window_minutes: float = 30.0  # candidates closer than this count as the same moment

    # Redundancy penalty against what is already chosen (hash similarity, section 4).
    redundancy_similarity: float = 0.8
    redundancy_penalty: float = 0.5

    # Use of `moment_type` in the edit.
    max_same_moment_run: int = 3
    transition_time_share: float = 0.10  # cap on the total time of `transition` clips
    transition_min_narrative_value: int = 3  # weaker `transition` clips are discarded
    moment_share_penalty: float = 0.3
    moment_shares: dict[str, float] = field(default_factory=_default_moment_shares)
    day_opener_bonus: float = 0.15  # the shot that opens a day (scenery, or calm without AI)

    # Spread over the vacation: no single file may monopolize the result.
    max_file_share: float = 0.2  # of `--duration`
    min_file_cap_seconds: float = 10.0  # the cap is never lower than this

    # Clip durations (seconds). Targets under `short_target_seconds` use the short ones.
    short_target_seconds: float = 60.0
    min_clip_seconds: float = 2.0
    photo_min_seconds: float = 3.0
    photo_max_seconds: float = 4.0
    video_min_seconds: float = 3.0
    video_max_seconds: float = 8.0
    video_shot_max_seconds: float = 15.0  # a video with more good moments gives fewer, longer continuous shots
    short_photo_min_seconds: float = 2.0
    short_photo_max_seconds: float = 3.0
    short_video_min_seconds: float = 2.0
    short_video_max_seconds: float = 4.0
    opening_clip_max_seconds: float = 3.0  # quick clips in the Opening
    detail_max_seconds: float = 3.0  # `detail` clips are a short rhythm break
    scenery_extra_seconds: float = 0.5  # `scenery` lasts slightly longer
    closing_photo_min_seconds: float = 4.0
    closing_photo_max_seconds: float = 5.0
    closing_video_min_seconds: float = 5.0
    closing_video_max_seconds: float = 8.0
    closing_short_min_seconds: float = 3.0  # Closing clip for short targets and the compressed structure
    closing_dissolve_seconds: float = 1.5  # the longer dissolve into the Closing

    # Narrative structure.
    sections: dict[str, SectionShare] = field(default_factory=_default_sections)
    min_section_seconds: float = 2.0  # a section with less budget than this is merged with its neighbor
    compressed_min_clips: int = 15  # with fewer useful clips the structure is compressed
    compressed_opening_max_seconds: float = 5.0

    # AI budget: at most this many analyzed frames per clip needed by the edit.
    ai_frames_per_clip: float = 3.0

    def to_dict(self) -> dict:
        data = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "sections":
                value = {name: vars(share) for name, share in value.items()}
            elif isinstance(value, dict):
                value = dict(value)
            data[item.name] = value
        return data


def selection_from_mapping(data: object) -> SelectionConfig:
    """Apply the `[selection]` table of the configuration file to the defaults; `ValueError` if invalid."""
    if not isinstance(data, Mapping):
        raise ValueError("[selection] must be a table")
    defaults = SelectionConfig()
    known = {item.name: item for item in fields(SelectionConfig)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ValueError(f"unknown key(s) in [selection]: {', '.join(unknown)}")

    changes: dict[str, object] = {}
    for key, value in data.items():
        if key == "sections":
            changes[key] = _sections(value, defaults.sections)
        elif key == "moment_shares":
            changes[key] = _moment_shares(value, defaults.moment_shares)
        elif isinstance(getattr(defaults, key), int):
            changes[key] = _number(value, f"selection.{key}", integer=True)
        else:
            changes[key] = _number(value, f"selection.{key}")
    config = replace(defaults, **changes)
    validate(config)
    return config


def validate(config: SelectionConfig) -> None:
    for name in ("technical_threshold", "max_black_fraction", "narrative_weight", "technical_weight",
                 "redundancy_similarity", "redundancy_penalty", "transition_time_share", "moment_share_penalty",
                 "max_file_share", "photo_motion"):
        if not 0 <= getattr(config, name) <= 1:
            raise ValueError(f"selection.{name} must be between 0 and 1")
    if abs(config.narrative_weight + config.technical_weight - 1) > 1e-6:
        raise ValueError("selection.narrative_weight and selection.technical_weight must add up to 1")
    if config.max_same_moment_run < 1 or config.compressed_min_clips < 1:
        raise ValueError("selection.max_same_moment_run and selection.compressed_min_clips must be at least 1")
    pairs = [("photo_min_seconds", "photo_max_seconds"), ("video_min_seconds", "video_max_seconds"),
             ("video_max_seconds", "video_shot_max_seconds"),
             ("short_photo_min_seconds", "short_photo_max_seconds"),
             ("short_video_min_seconds", "short_video_max_seconds"),
             ("closing_photo_min_seconds", "closing_photo_max_seconds"),
             ("closing_video_min_seconds", "closing_video_max_seconds")]
    for low, high in pairs:
        if not config.min_clip_seconds <= getattr(config, low) <= getattr(config, high):
            raise ValueError(f"selection.{low} must be at least min_clip_seconds and at most {high}")
    total = sum(section.share for section in config.sections.values())
    if abs(total - 1) > 0.01:
        raise ValueError(f"the shares of the sections must add up to 1, they add up to {total:g}")
    for name, section in config.sections.items():
        if not 0 <= section.min <= section.share <= section.max <= 1:
            raise ValueError(f"selection.sections.{name}: expected 0 <= min <= share <= max <= 1")


def _number(value: object, where: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{where} must be an integer")
    if value < 0 or value != value or value == float("inf"):
        raise ValueError(f"{where} must be a finite number, not negative")
    return value


def _sections(value: object, defaults: dict[str, SectionShare]) -> dict[str, SectionShare]:
    if not isinstance(value, Mapping):
        raise ValueError("[selection.sections] must be a table")
    unknown = sorted(set(value) - set(SECTIONS))
    if unknown:
        raise ValueError(f"unknown section(s) in [selection.sections]: {', '.join(unknown)}")
    result = dict(defaults)
    for name, table in value.items():
        if not isinstance(table, Mapping) or not set(table) <= {"share", "min", "max"}:
            raise ValueError(f"[selection.sections.{name}] accepts only share, min and max")
        current = vars(defaults[name])
        result[name] = SectionShare(**{
            key: _number(table.get(key, current[key]), f"selection.sections.{name}.{key}") for key in current
        })
    return result


def _moment_shares(value: object, defaults: dict[str, float]) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("[selection.moment_shares] must be a table")
    unknown = sorted(set(value) - set(MOMENT_TYPES))
    if unknown:
        raise ValueError(f"unknown moment type(s) in [selection.moment_shares]: {', '.join(unknown)}")
    return {**defaults, **{key: _number(share, f"selection.moment_shares.{key}") for key, share in value.items()}}
