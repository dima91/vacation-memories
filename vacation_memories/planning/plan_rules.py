"""The editing rules of a plan, in one place: limits, checks and duration (section 12 of `UI-SPEC.md`).

The interface does not reimplement these rules. The server computes the limits of every element with
`plan_limits` and sends them next to the plan, so the editor can give immediate feedback while the
user drags; on every save the server runs `check_plan` again, so a client that ignores the limits (or
a hand-edited plan) cannot save what the rules forbid.

Everything is on top of `transitions.layout_timeline`, the only layout and duration function: the
duration of a plan is `plan_duration`, which is what the planner writes as `planned_duration` and
what the renderer builds, with the dissolves overlapping their clips (decision D2).

Limits, with the decisions of section 16:
- a photo lasts 1 to 30 s;
- a video moment lasts at least 1 s, stays inside its video and does not overlap the other moments of
  the same video;
- `fade_in`, `fade_out` and `dissolve` stay at the edges of their clip, `dip` is the only effect that
  can sit in the middle (D1); an effect stays inside its clip, does not overlap the other effects of
  the clip (the dissolve into the next clip counts, it covers the end of this one), and an effect of
  the user lasts at least `rules.min_seconds` (0.2 s by default); a dissolve must fit in the free space
  of both clips it joins. The dissolves of the planner keep the 25% limit of the specification (D3);
- the music shift leaves at least a second of music in the video, and a part of a track lasts at least a second.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vacation_memories.planning.edit_plan import (
    DIP,
    DISSOLVE,
    EFFECT_TYPES,
    FADE_IN,
    FADE_OUT,
    PHOTO,
    VIDEO,
    EditPlan,
    MusicPlan,
    MusicTrack,
    PlanElement,
    PlanError,
    plan_layout,
)
from vacation_memories.planning.transitions import PLANNER, USER, TransitionRules

PHOTO_MIN_SECONDS = 1.0
PHOTO_MAX_SECONDS = 30.0
TIME_STEP_SECONDS = 0.1  # the precision of the user's edits
MOMENT_MIN_SECONDS = 1.0
MUSIC_MIN_AUDIBLE_SECONDS = 1.0
MUSIC_MIN_PART_SECONDS = 1.0
TOLERANCE = 1e-3  # seconds: what the values of a saved plan may differ from the exact geometry

Window = tuple[float, float]


@dataclass(frozen=True)
class Limits:
    minimum: float
    maximum: float
    step: float = TIME_STEP_SECONDS

    def to_dict(self) -> dict:
        return {"min": self.minimum, "max": self.maximum, "step": self.step}


@dataclass(frozen=True)
class TrimLimits:
    """Where a moment can start and end: `end_max` is `None` when the length of the video is not known."""

    start_min: float
    end_max: float | None
    min_length: float = MOMENT_MIN_SECONDS

    def to_dict(self) -> dict:
        return {"start_min": self.start_min, "end_max": self.end_max, "min_length": self.min_length}


@dataclass(frozen=True)
class EffectRoom:
    """The stretches of a clip (seconds from its start) where an effect of a type can sit.

    `fade_in` and `dissolve` start at the clip's start and `fade_out` ends at its end, so for them the
    window is also the longest they can last; a `dip` can slide inside any window. Windows shorter than
    the minimum are not listed: no window means the effect cannot be added.
    """

    type: str
    windows: tuple[Window, ...]

    @property
    def max_duration(self) -> float:
        return max((end - start for start, end in self.windows), default=0.0)

    def to_dict(self) -> dict:
        return {"type": self.type, "windows": [list(window) for window in self.windows]}


@dataclass(frozen=True)
class Violation:
    path: str  # where in the plan, as the loader names it: `elements[3].effects[0]`
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class MusicSlot:
    """A part of a track on the video's time axis (after the shift): it may start before 0 or end after the video."""

    track: int
    part: int
    file: str
    start: float
    end: float
    muted: bool


@dataclass(frozen=True)
class ElementLimits:
    id: str
    kind: str
    duration: Limits | None  # photos
    trim: TrimLimits | None  # video moments
    effects: dict[str, EffectRoom]  # for each effect of the clip, the room it has (itself left out)
    available: dict[str, EffectRoom]  # for each type, where a new one could go

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "duration": None if self.duration is None else self.duration.to_dict(),
            "trim": None if self.trim is None else self.trim.to_dict(),
            "effects": {identifier: room.to_dict() for identifier, room in self.effects.items()},
            "available": {kind: room.to_dict() for kind, room in self.available.items()},
        }


@dataclass(frozen=True)
class PlanLimits:
    elements: dict[str, ElementLimits]
    min_effect_seconds: float
    music_offset: Limits | None
    min_part_seconds: float = MUSIC_MIN_PART_SECONDS

    def to_dict(self) -> dict:
        return {
            "min_effect_seconds": self.min_effect_seconds,
            "elements": {identifier: limits.to_dict() for identifier, limits in self.elements.items()},
            "music": None if self.music_offset is None
            else {"offset": self.music_offset.to_dict(), "min_part": self.min_part_seconds},
        }


# --- duration ---------------------------------------------------------------------------------------


def plan_duration(plan: EditPlan, rules: TransitionRules) -> float:
    """Seconds of the video: the clips minus the overlaps of the dissolves, from the shared layout."""
    return round(plan_layout(plan, rules).duration, 6) if plan.elements else 0.0


# --- photos and moments ---------------------------------------------------------------------------


def photo_limits() -> Limits:
    return Limits(PHOTO_MIN_SECONDS, PHOTO_MAX_SECONDS)


def trim_limits(elements: Sequence[PlanElement], index: int, media_duration: float | None = None) -> TrimLimits:
    """The bounds of a video moment: inside its video, without overlapping the neighboring moments."""
    element = elements[index]
    others = [other for position, other in enumerate(elements)
              if position != index and other.kind == VIDEO and other.source == element.source]
    before = max((other.end for other in others if other.end <= element.start + TOLERANCE), default=0.0)
    after = min((other.start for other in others if other.start >= element.end - TOLERANCE), default=None)
    ends = [value for value in (after, media_duration) if value is not None]
    return TrimLimits(max(0.0, before), min(ends) if ends else None)


# --- effects -------------------------------------------------------------------------------------------


def effect_room(
    elements: Sequence[PlanElement], index: int, kind: str, rules: TransitionRules, *, ignore: str | None = None
) -> EffectRoom:
    """Where an effect of `kind` fits on clip `index`; `ignore` is the id of the effect being moved or resized.

    For a dissolve, `index` is the incoming clip and the room is the free space of both clips.
    """
    element = elements[index]
    taken = _taken(elements, index, ignore)
    if kind == FADE_IN:
        windows = [(0.0, min((start for start, _ in taken), default=element.duration))]
    elif kind == FADE_OUT:
        windows = [(max((end for _, end in taken), default=0.0), element.duration)]
    elif kind == DISSOLVE:
        windows = []
        if index > 0:
            head = min((start for start, _ in taken), default=element.duration)
            previous = elements[index - 1]
            tail = previous.duration - max((end for _, end in _taken(elements, index - 1, ignore)), default=0.0)
            windows = [(0.0, min(head, tail))]
    elif kind == DIP:
        windows = _gaps(taken, element.duration)
    else:
        raise ValueError(f"unknown effect type {kind!r}")
    fitting = tuple((round(start, 6), round(end, 6)) for start, end in windows
                    if end - start >= rules.min_seconds - TOLERANCE)
    return EffectRoom(kind, fitting)


def _taken(elements: Sequence[PlanElement], index: int, ignore: str | None) -> list[Window]:
    """The stretches of clip `index` that effects occupy, the dissolve into the next clip included."""
    element = elements[index]
    taken = [(effect.offset, effect.end) for effect in element.effects if effect.id != ignore]
    if index + 1 < len(elements):
        for effect in elements[index + 1].effects:
            if effect.type == DISSOLVE and effect.id != ignore:
                taken.append((max(0.0, element.duration - effect.duration), element.duration))
    return sorted(taken)


def _gaps(taken: list[Window], length: float) -> list[Window]:
    gaps, cursor = [], 0.0
    for start, end in taken:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if length > cursor:
        gaps.append((cursor, length))
    return gaps


# --- music -----------------------------------------------------------------------------------------------


def music_slots(music: MusicPlan) -> list[MusicSlot]:
    """The parts one after the other on the video's time axis, the shift (`offset`) applied.

    Muted parts take their time: they only make no sound. Deleting a part shifts the following ones.
    """
    slots, cursor = [], music.offset
    for number, track in enumerate(music.tracks):
        for position, part in enumerate(track.parts):
            slots.append(MusicSlot(number, position, track.file, cursor, cursor + part.length, part.muted))
            cursor += part.length
    return slots


def music_length(music: MusicPlan) -> float:
    return sum(part.length for track in music.tracks for part in track.parts)


def music_offset_limits(music: MusicPlan, video_duration: float) -> Limits:
    """The shift of the music: at least a second of it must stay in the video, from either end."""
    low = -max(0.0, music_length(music) - MUSIC_MIN_AUDIBLE_SECONDS)
    high = max(0.0, video_duration - MUSIC_MIN_AUDIBLE_SECONDS)
    return Limits(low, high)


def remove_part(music: MusicPlan, track: int, part: int) -> MusicPlan:
    """Delete a part: the following ones shift to fill its place, and a track left without parts goes away."""
    tracks = []
    for number, current in enumerate(music.tracks):
        if number != track:
            tracks.append(current)
            continue
        parts = tuple(item for position, item in enumerate(current.parts) if position != part)
        if parts:
            tracks.append(MusicTrack(current.file, parts))
    return MusicPlan(tuple(tracks), music.offset)


# --- limits of a whole plan ---------------------------------------------------------------------------------


def plan_limits(
    plan: EditPlan,
    rules: TransitionRules,
    *,
    media_durations: Mapping[str, float] | None = None,
) -> PlanLimits:
    """The limits of every element, to send next to the plan (they are never written into it).

    `media_durations` maps a source (relative path) to the length of its video, when it is known.
    """
    elements = plan.elements
    media_durations = media_durations or {}
    result = {}
    for index, element in enumerate(elements):
        result[element.id] = ElementLimits(
            id=element.id,
            kind=element.kind,
            duration=photo_limits() if element.kind == PHOTO else None,
            trim=trim_limits(elements, index, media_durations.get(element.source)) if element.kind == VIDEO else None,
            effects={effect.id: effect_room(elements, index, effect.type, rules, ignore=effect.id)
                     for effect in element.effects},
            available={kind: effect_room(elements, index, kind, rules) for kind in EFFECT_TYPES},
        )
    music = None if plan.music is None else music_offset_limits(plan.music, plan_duration(plan, rules))
    return PlanLimits(result, rules.min_seconds, music)


# --- checks ----------------------------------------------------------------------------------------------------


def check_plan(
    plan: EditPlan,
    rules: TransitionRules,
    *,
    media_durations: Mapping[str, float] | None = None,
    track_durations: Mapping[str, float] | None = None,
) -> list[Violation]:
    """Every rule the plan breaks, with its place; an empty list means the plan respects the limits.

    `media_durations` and `track_durations` (by source and by music file) are optional: a bound that
    needs a length that is not known is not checked.
    """
    violations: list[Violation] = []
    elements = plan.elements
    media_durations = media_durations or {}
    for index, element in enumerate(elements):
        where = f"elements[{index}]"
        if element.kind == PHOTO:
            _check_photo(element, where, violations)
        else:
            _check_moment(elements, index, where, media_durations.get(element.source), violations)
        _check_effects(elements, index, where, rules, violations)
    if plan.music is not None:
        _check_music(plan, rules, track_durations or {}, violations)
    return violations


def ensure_valid(plan: EditPlan, rules: TransitionRules, **known_lengths) -> None:
    """Raise a `PlanError` (exit code 2) naming the first rule the plan breaks."""
    violations = check_plan(plan, rules, **known_lengths)
    if violations:
        more = f" (and {len(violations) - 1} more)" if len(violations) > 1 else ""
        raise PlanError(f"{violations[0]}{more}")


def _check_photo(element: PlanElement, where: str, violations: list[Violation]) -> None:
    if element.duration_origin != USER:
        return  # the planner's durations follow the selection configuration, not the editor's limits
    if not PHOTO_MIN_SECONDS - TOLERANCE <= element.duration <= PHOTO_MAX_SECONDS + TOLERANCE:
        violations.append(Violation(
            f"{where}.duration",
            f"a photo lasts between {PHOTO_MIN_SECONDS:g} and {PHOTO_MAX_SECONDS:g} s, got {element.duration:g}",
        ))


def _check_moment(
    elements: Sequence[PlanElement], index: int, where: str, media_duration: float | None, violations: list[Violation]
) -> None:
    element = elements[index]
    if element.duration < MOMENT_MIN_SECONDS - TOLERANCE:
        violations.append(Violation(where, f"a moment lasts at least {MOMENT_MIN_SECONDS:g} s, got {element.duration:g}"))
    if media_duration is not None and element.end > media_duration + TOLERANCE:
        violations.append(Violation(f"{where}.end", f"the moment ends after the video, which lasts {media_duration:g} s"))
    for position, other in enumerate(elements):
        if position <= index or other.kind != VIDEO or other.source != element.source:
            continue
        if element.start < other.end - TOLERANCE and other.start < element.end - TOLERANCE:
            violations.append(Violation(
                where, f"overlaps the moment elements[{position}] of the same video "
                       f"({other.start:g}-{other.end:g} s)",
            ))


def _check_effects(
    elements: Sequence[PlanElement], index: int, where: str, rules: TransitionRules, violations: list[Violation]
) -> None:
    element = elements[index]
    length = element.duration
    for number, effect in enumerate(element.effects):
        here = f"{where}.effects[{number}]"
        if effect.offset < -TOLERANCE or effect.end > length + TOLERANCE:
            violations.append(Violation(here, f"the {effect.type} does not fit in the clip, which lasts {length:g} s"))
            continue
        if effect.type in (FADE_IN, DISSOLVE) and abs(effect.offset) > TOLERANCE:
            violations.append(Violation(here, f"a {effect.type} starts at the start of the clip (offset 0)"))
        if effect.type == FADE_OUT and abs(effect.end - length) > TOLERANCE:
            violations.append(Violation(here, "a fade_out ends at the end of the clip"))
        if effect.origin == USER and effect.duration < rules.min_seconds - TOLERANCE:
            violations.append(Violation(here, f"an effect lasts at least {rules.min_seconds:g} s, got {effect.duration:g}"))
        if effect.type == DISSOLVE:
            _check_dissolve(elements, index, number, here, rules, violations)
    _check_overlaps(elements, index, where, violations)


def _check_dissolve(
    elements: Sequence[PlanElement], index: int, number: int, here: str, rules: TransitionRules,
    violations: list[Violation],
) -> None:
    effect = elements[index].effects[number]
    if index == 0:
        violations.append(Violation(here, "the first clip has no clip before it to dissolve from"))
        return
    previous = elements[index - 1]
    if effect.duration > previous.duration + TOLERANCE:
        violations.append(Violation(here, f"the dissolve does not fit in the previous clip, which lasts {previous.duration:g} s"))
    elif effect.origin == PLANNER:
        limit = rules.max_fraction * min(previous.duration, elements[index].duration)
        if effect.duration > limit + TOLERANCE:
            violations.append(Violation(
                here, f"a dissolve of the planner lasts at most {rules.max_fraction:.0%} of the shorter clip ({limit:g} s)",
            ))


def _check_overlaps(elements: Sequence[PlanElement], index: int, where: str, violations: list[Violation]) -> None:
    element = elements[index]
    labelled = [(effect.offset, effect.end, f"{where}.effects[{number}]", f"{effect.type} effect {effect.id!r}")
                for number, effect in enumerate(element.effects)]
    if index + 1 < len(elements):
        for effect in elements[index + 1].effects:
            if effect.type == DISSOLVE:
                start = max(0.0, element.duration - effect.duration)
                labelled.append((start, element.duration, f"{where}", "dissolve into the next clip"))
    for first in range(len(labelled)):
        for second in range(first + 1, len(labelled)):
            a, b = labelled[first], labelled[second]
            if a[0] < b[1] - TOLERANCE and b[0] < a[1] - TOLERANCE:
                path, other = (a[2], b[3]) if a[2] != where else (b[2], a[3])
                violations.append(Violation(path, f"overlaps the {other}"))


def _check_music(
    plan: EditPlan, rules: TransitionRules, track_durations: Mapping[str, float], violations: list[Violation]
) -> None:
    music = plan.music
    for number, track in enumerate(music.tracks):
        known = track_durations.get(track.file)
        previous_end = 0.0
        for position, part in enumerate(track.parts):
            here = f"music.tracks[{number}].parts[{position}]"
            if part.start < previous_end - TOLERANCE:
                violations.append(Violation(here, "the parts of a track are in order and do not overlap"))
            previous_end = max(previous_end, part.end)
            if known is not None and part.end > known + TOLERANCE:
                violations.append(Violation(f"{here}.end", f"the part ends after the track, which lasts {known:g} s"))
            if part.length < MUSIC_MIN_PART_SECONDS - TOLERANCE:
                violations.append(Violation(here, f"a part of the music lasts at least {MUSIC_MIN_PART_SECONDS:g} s"))
            if part.fade_in + part.fade_out > part.length + TOLERANCE:
                violations.append(Violation(here, "the fade-in and the fade-out do not fit in the part"))
    limits = music_offset_limits(music, plan_duration(plan, rules))
    if not limits.minimum - TOLERANCE <= music.offset <= limits.maximum + TOLERANCE:
        violations.append(Violation(
            "music.offset",
            f"the shift of the music is between {limits.minimum:g} and {limits.maximum:g} s, got {music.offset:g}",
        ))
