"""`edit_plan.json`: the hand-editable plan the renderer works from (schema 2, section 8 of `UI-SPEC.md`).

Times of a clip (`start`, `end`) are seconds from its first frame, that is relative to the stream
as FFprobe exposes it (a container whose first timestamp is not zero does not shift them).
`source` is the path relative to `--input`.

Every element carries what the planner knows (scores, moment type, category, section, description,
reason, and the capture date with its source and, for the `mtime` fallback, the assumed time zone),
a stable `id`, its `origin` (`planner` or `user`) and its `effects` (`fade_in`, `fade_out`,
`dissolve`, `dip`). At plan level, `excluded` lists the moments the user deleted, which the planner
does not choose again, and `music` the tracks with their parts and the `offset`. The renderer needs
only `source`, `type`, the extent and the dissolve of each element (`PlanElement.transition_in`): a
transition written by hand is not trusted, the renderer reduces it to the limits of section 8.
Unknown keys are rejected, so a typo in a hand-edited plan is an error and not a silent no-op.

A schema 1 plan is migrated when it is read (`transition_in` becomes a `dissolve` effect, the global
fades become the effects of the first and last element) and is written as schema 2 at the next save.
Whatever is a limit of the editor lives in `plan_rules`, never in the plan.
"""

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path, PurePosixPath

from vacation_memories.core.atomic import atomic_write_bytes, atomic_write_json
from vacation_memories.core.config import DEFAULT_FADE_IN_SECONDS, DEFAULT_FADE_OUT_SECONDS, ConfigError
from vacation_memories.planning.transitions import (
    CUT,
    DISSOLVE,
    MIN_DISSOLVE_SECONDS,
    ORIGINS,
    PLANNER,
    TRANSITION_TYPES,
    USER,
    Layout,
    TransitionRequest,
    TransitionRules,
    layout_timeline,
    seconds_to_frames,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
SUPPORTED_VERSIONS = (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION)
PHOTO = "photo"
VIDEO = "video"
FADE_IN = "fade_in"  # from black, at the start of a clip
FADE_OUT = "fade_out"  # to black, at the end of a clip
DIP = "dip"  # to black and back, anywhere in a clip
EFFECT_TYPES = (FADE_IN, FADE_OUT, DISSOLVE, DIP)
EXCLUDED_BY_USER = "user"
HISTORY_DIR = "plan_history"
HISTORY_KEEP = 10
MAX_FPS = 240
NTSC_NUMERATORS = (24000, 30000, 48000, 60000, 120000)
NTSC_TOLERANCE = 0.005
SCORE_KEYS = ("final", "technical_score", "narrative_score", "components")
DESCRIPTIVE_KEYS = (
    "moment_type", "category", "section", "description", "reason", "captured_at", "date_source", "assumed_tz",
)
_COMMON_ELEMENT_KEYS = {"source", "type", "start", "end", "duration", "score", "timestamp", *DESCRIPTIVE_KEYS}
_LEGACY_ELEMENT_KEYS = _COMMON_ELEMENT_KEYS | {"transition_in"}
_ELEMENT_KEYS = _COMMON_ELEMENT_KEYS | {"id", "origin", "duration_origin", "effects"}
_LEGACY_PLAN_KEYS = {
    "schema_version", "requested_duration", "planned_duration", "output", "assumed_tz", "runtime", "elements",
}
_PLAN_KEYS = _LEGACY_PLAN_KEYS | {"excluded", "music"}
_EFFECT_ID = re.compile(r"fx-(\d+)$")
_EPSILON = 1e-6


class PlanError(ConfigError):
    """The plan is invalid or unsupported (exit code 2)."""


@dataclass(frozen=True)
class OutputSpec:
    width: int
    height: int
    fps: float = 30.0

    @property
    def frame_rate(self) -> Fraction:
        """The frame rate as an exact fraction: the NTSC rates (23.976, 29.97, 59.94) are n*1000/1001."""
        for numerator in NTSC_NUMERATORS:
            if abs(self.fps - numerator / 1001) < NTSC_TOLERANCE:
                return Fraction(numerator, 1001)
        return Fraction(str(self.fps)).limit_denominator(1000)


@dataclass(frozen=True)
class Effect:
    """A transition on a clip: `offset` seconds from the start of the clip, lasting `duration`.

    A dissolve is stored once, in the incoming clip with `offset: 0` (it also covers the end of the
    previous clip); `fade_in` and `fade_out` stay at the edges of the clip, `dip` can be anywhere.
    """

    id: str
    type: str
    offset: float
    duration: float
    origin: str = PLANNER

    @property
    def end(self) -> float:
        return self.offset + self.duration

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "offset": self.offset, "duration": self.duration,
                "origin": self.origin}


@dataclass(frozen=True)
class Exclusion:
    """A moment the user deleted (or a photo they hid): the planner does not select it again.

    `start` and `end` are the extent of the moment in its video; both are `None` for a photo, and for
    a whole file.
    """

    source: str
    start: float | None = None
    end: float | None = None
    reason: str = EXCLUDED_BY_USER

    def to_dict(self) -> dict:
        return {"source": self.source, "start": self.start, "end": self.end, "reason": self.reason}


@dataclass(frozen=True)
class MusicPart:
    """A stretch of a track, `start` and `end` in seconds from the start of the track."""

    start: float
    end: float
    muted: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0

    @property
    def length(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "muted": self.muted, "fade_in": self.fade_in,
                "fade_out": self.fade_out}


@dataclass(frozen=True)
class MusicTrack:
    file: str  # relative to the music root
    parts: tuple[MusicPart, ...]

    def to_dict(self) -> dict:
        return {"file": self.file, "parts": [part.to_dict() for part in self.parts]}


@dataclass(frozen=True)
class MusicPlan:
    """The tracks in order, each cut into parts, and the shift of the whole music against the video.

    A positive `offset` makes the music start later, a negative one skips its beginning. A plan with
    no `music` block at all uses `--music` instead; a block with no tracks is a video without music.
    """

    tracks: tuple[MusicTrack, ...] = ()
    offset: float = 0.0

    def to_dict(self) -> dict:
        return {"tracks": [track.to_dict() for track in self.tracks], "offset": self.offset}


def default_element_id(source: str, start: float | None, end: float | None) -> str:
    """The id of an element that has none: the source, and for a clip its extent (as a candidate id)."""
    return source if start is None else f"{source}#{start:.3f}-{end:.3f}"


@dataclass(frozen=True)
class PlanElement:
    source: str
    kind: str  # "photo" or "video"
    duration: float
    start: float | None = None  # clips only
    end: float | None = None
    score: dict | None = None
    moment_type: str | None = None
    category: str | None = None
    section: str | None = None
    description: str | None = None
    reason: str | None = None
    captured_at: str | None = None  # naive ISO date and time, as written in the metadata
    date_source: str | None = None  # "exif", "container" or "mtime"
    assumed_tz: str | None = None  # only for the mtime fallback
    timestamp: float | None = None  # destination, computed by `finalize_timeline`
    id: str = ""  # unique in the plan; the candidate id, or `user-<n>`; it does not change when the clip is trimmed
    origin: str = PLANNER
    duration_origin: str | None = None  # photos only: whether the duration is the planner's or the user's
    effects: tuple[Effect, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", default_element_id(self.source, self.start, self.end))
        if self.kind == PHOTO and self.duration_origin is None:
            object.__setattr__(self, "duration_origin", PLANNER)
        object.__setattr__(self, "effects", tuple(self.effects))

    @property
    def transition_in(self) -> TransitionRequest:
        """What the renderer and the layout read: the dissolve into this clip, if it has one."""
        for effect in self.effects:
            if effect.type == DISSOLVE:
                return TransitionRequest(DISSOLVE, effect.duration, effect.origin)
        return TransitionRequest()

    def to_dict(self) -> dict:
        data: dict = {"id": self.id, "origin": self.origin, "source": self.source, "type": self.kind}
        if self.kind == VIDEO:
            data["start"] = self.start
            data["end"] = self.end
        data["duration"] = self.duration
        if self.kind == PHOTO:
            data["duration_origin"] = self.duration_origin
        for key in DESCRIPTIVE_KEYS:
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        if self.score is not None:
            data["score"] = self.score
        data["effects"] = [effect.to_dict() for effect in self.effects]
        if self.timestamp is not None:
            data["timestamp"] = self.timestamp
        return data


@dataclass(frozen=True)
class EdgeFades:
    """Seconds of the fade from black at the start of the video and of the fade to black at its end."""

    fade_in: float = DEFAULT_FADE_IN_SECONDS
    fade_out: float = DEFAULT_FADE_OUT_SECONDS


@dataclass(frozen=True)
class EditPlan:
    output: OutputSpec
    elements: list[PlanElement]
    requested_duration: float | None = None
    planned_duration: float | None = None
    assumed_tz: str | None = None
    runtime: dict | None = None
    schema_version: int = SCHEMA_VERSION
    excluded: tuple[Exclusion, ...] = ()
    music: MusicPlan | None = None  # None: the music of `--music`, if any

    def to_dict(self) -> dict:
        data: dict = {
            "schema_version": self.schema_version,
            "requested_duration": self.requested_duration,
            "planned_duration": self.planned_duration,
            "output": {"width": self.output.width, "height": self.output.height, "fps": self.output.fps},
            "assumed_tz": self.assumed_tz,
        }
        if self.runtime is not None:
            data["runtime"] = self.runtime
        data["elements"] = [element.to_dict() for element in self.elements]
        data["excluded"] = [exclusion.to_dict() for exclusion in self.excluded]
        if self.music is not None:
            data["music"] = self.music.to_dict()
        return data


def save_plan(plan: EditPlan, path: Path) -> None:
    atomic_write_json(path, plan.to_dict())


def load_plan(path: Path, *, fades: EdgeFades | None = None) -> EditPlan:
    """Read a plan; a schema 1 plan is migrated (`fades` are the global fades it implied)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlanError(f"plan not found: {path}") from None
    except OSError as error:
        raise PlanError(f"cannot read the plan {path}: {error.strerror}") from None
    try:
        data = json.loads(text)
    except ValueError as error:
        raise PlanError(f"{path} is not valid JSON: {error}") from None
    try:
        return plan_from_dict(data, fades=fades)
    except PlanError as error:
        raise PlanError(f"invalid plan {path}: {error}") from None


def plan_from_dict(data: object, *, fades: EdgeFades | None = None) -> EditPlan:
    if not isinstance(data, dict):
        raise PlanError("the plan must be a JSON object")
    version = data.get("schema_version")
    if version not in SUPPORTED_VERSIONS or isinstance(version, bool):
        supported = " and ".join(map(str, SUPPORTED_VERSIONS))
        raise PlanError(f"unsupported schema_version {version!r}: this version supports {supported}")
    legacy = version == LEGACY_SCHEMA_VERSION
    _reject_unknown(data, _LEGACY_PLAN_KEYS if legacy else _PLAN_KEYS, "the plan")

    output = _output(data.get("output"))
    elements_data = data.get("elements")
    if not isinstance(elements_data, list):
        raise PlanError("'elements' must be a list")
    elements = [_element(item, f"elements[{index}]", output, legacy) for index, item in enumerate(elements_data)]
    if legacy:
        elements = _migrate(elements, fades or EdgeFades())
    else:
        _check_unique_ids(elements)

    runtime = data.get("runtime")
    if runtime is not None and not isinstance(runtime, dict):
        raise PlanError("'runtime' must be an object")
    assumed_tz = data.get("assumed_tz")
    if assumed_tz is not None and not isinstance(assumed_tz, str):
        raise PlanError("'assumed_tz' must be a string")
    return EditPlan(
        output=output,
        elements=elements,
        requested_duration=_optional_number(data, "requested_duration", "the plan", minimum=0),
        planned_duration=_optional_number(data, "planned_duration", "the plan", minimum=0),
        assumed_tz=assumed_tz,
        runtime=runtime,
        excluded=() if legacy else _exclusions(data.get("excluded")),
        music=None if legacy else _music(data.get("music")),
    )


# --- layout and effects ---------------------------------------------------------------------------


def finalize_timeline(plan: EditPlan, rules: TransitionRules | None = None) -> tuple[EditPlan, Layout]:
    """Apply the transition limits and fill in the destination timestamps and `planned_duration`.

    The dissolve effects take the effective length the layout gives them (a dissolve turned into a
    cut disappears). This is what the planner calls before saving; the renderer calls the same layout
    function.
    """
    rules = rules or TransitionRules()
    layout = plan_layout(plan, rules)
    elements = []
    for element, placement in zip(plan.elements, layout.placements):
        effects = []
        for effect in element.effects:
            if effect.type != DISSOLVE:
                effects.append(effect)
            elif placement.dissolve_frames:
                effects.append(Effect(effect.id, DISSOLVE, 0.0, layout.seconds(placement.dissolve_frames), effect.origin))
        elements.append(_replace(element, effects=tuple(effects), timestamp=layout.seconds(placement.start_frame)))
    planned = round(layout.duration, 6) if elements else 0.0
    return _replace_plan(plan, elements=elements, planned_duration=planned), layout


def plan_layout(plan: EditPlan, rules: TransitionRules) -> Layout:
    fps = plan.output.frame_rate
    frames = [max(1, seconds_to_frames(element.duration, fps)) for element in plan.elements]
    return layout_timeline(frames, [element.transition_in for element in plan.elements], fps, rules)


def add_edge_fades(
    elements: list[PlanElement], fades: EdgeFades, *, min_seconds: float = MIN_DISSOLVE_SECONDS
) -> list[PlanElement]:
    """The fade from black as an effect of the first element, the fade to black as one of the last.

    A fade stays inside its clip and does not overlap the other effects of it (including the dissolve
    into the next clip), so it is shortened to the free space, and left out below `min_seconds`.
    An element that already has the effect is left alone.
    """
    elements = list(elements)
    if not elements:
        return elements
    for index, kind, wanted in ((0, FADE_IN, fades.fade_in), (len(elements) - 1, FADE_OUT, fades.fade_out)):
        element = elements[index]
        if wanted <= 0 or any(effect.type == kind for effect in element.effects):
            continue
        following = elements[index + 1] if index + 1 < len(elements) else None
        duration = round(min(wanted, _edge_room(element, kind, following)), 6)
        if duration < min_seconds - _EPSILON:
            continue
        offset = 0.0 if kind == FADE_IN else round(element.duration - duration, 6)
        effect = Effect(_next_effect_id(elements), kind, offset, duration, PLANNER)
        elements[index] = _replace(element, effects=(*element.effects, effect))
    return elements


def _edge_room(element: PlanElement, kind: str, following: PlanElement | None) -> float:
    taken = [(effect.offset, effect.end) for effect in element.effects]
    if following is not None and following.transition_in.type == DISSOLVE:
        taken.append((element.duration - following.transition_in.duration, element.duration))
    if kind == FADE_IN:
        return min((start for start, _ in taken), default=element.duration)
    return element.duration - max((end for _, end in taken), default=0.0)


def _next_effect_id(elements: list[PlanElement]) -> str:
    numbers = [int(match.group(1)) for element in elements for effect in element.effects
               if (match := _EFFECT_ID.match(effect.id))]
    return f"fx-{max(numbers, default=0) + 1}"


def _replace(element: PlanElement, **changes) -> PlanElement:
    return PlanElement(**{**element.__dict__, **changes})


def _replace_plan(plan: EditPlan, **changes) -> EditPlan:
    return EditPlan(**{**plan.__dict__, **changes})


# --- regeneration: what the user edited, and the copies in plan_history/ ---------------------------


def has_user_edits(plan: EditPlan) -> bool:
    """Whether the plan holds anything a new plan would overwrite that the planner did not write."""
    return bool(
        plan.excluded
        or plan.music is not None
        or any(
            element.origin == USER or element.duration_origin == USER
            or any(effect.origin == USER for effect in element.effects)
            for element in plan.elements
        )
    )


def keep_user_edits(plan_path: Path, *, now: datetime | None = None) -> tuple[Exclusion, ...]:
    """Before a new plan overwrites `plan_path`: copy it into `plan_history/` if it holds user edits.

    Returns the exclusions to give the planner. A plan that cannot be read or is invalid may be a
    hand-edited one, so it is copied too (and no exclusions are known). Raises when the copy fails,
    so that nothing is overwritten that was not saved first.
    """
    plan_path = Path(plan_path)
    try:
        raw = plan_path.read_bytes()
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise PlanError(f"cannot read the plan {plan_path} to back it up: {error.strerror}") from None
    try:
        previous = plan_from_dict(json.loads(raw.decode("utf-8")))
    except (ValueError, PlanError) as error:
        logger.warning("the existing plan cannot be read (%s): it is copied into %s before it is replaced",
                       error, HISTORY_DIR)
        _back_up(plan_path, raw, now)
        return ()
    if not has_user_edits(previous):
        return ()
    saved = _back_up(plan_path, raw, now)
    logger.info("the existing plan has user edits: copied to %s; %d exclusion(s) are kept", saved,
                len(previous.excluded))
    return previous.excluded


def back_up_plan(plan_path: Path, *, now: datetime | None = None) -> Path | None:
    """Copy the plan into `plan_history/` whatever it holds (the editor's "overwrite"); None if there is none."""
    plan_path = Path(plan_path)
    try:
        raw = plan_path.read_bytes()
    except FileNotFoundError:
        return None
    return _back_up(plan_path, raw, now)


def _back_up(plan_path: Path, raw: bytes, now: datetime | None) -> Path:
    folder = plan_path.parent / HISTORY_DIR
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        moment = now or datetime.now(timezone.utc)
        while True:  # the name sorts like the time, so pruning keeps the newest
            target = folder / f"{plan_path.stem}-{moment.strftime('%Y%m%dT%H%M%S%fZ')}{plan_path.suffix}"
            if not target.exists():
                break
            moment = moment.replace(microsecond=(moment.microsecond + 1) % 1_000_000)
        atomic_write_bytes(target, raw)
        _prune(folder, plan_path)
    except OSError as error:
        raise PlanError(f"cannot copy the plan into {folder}: {error.strerror or error}") from None
    return target


def _prune(folder: Path, plan_path: Path, keep: int = HISTORY_KEEP) -> None:
    pattern = re.compile(rf"{re.escape(plan_path.stem)}-\d{{8}}T\d{{12}}Z{re.escape(plan_path.suffix)}$")
    copies = sorted(path for path in folder.iterdir() if pattern.match(path.name))
    for old in copies[:-keep]:
        old.unlink(missing_ok=True)


# --- migration from schema 1 -----------------------------------------------------------------------


def _migrate(elements: list[PlanElement], fades: EdgeFades) -> list[PlanElement]:
    """Schema 1 to 2: stable ids, `transition_in` as a dissolve effect, the global fades as effects.

    The dissolve into the first element is dropped: there is nothing before it, and the layout
    ignored it as well. Ids come from the source and the extent, made unique when a clip appears twice.
    """
    migrated, taken, counter = [], set(), 0
    for index, element in enumerate(elements):
        identifier, number = element.id, 2
        while identifier in taken:
            identifier = f"{element.id}~{number}"
            number += 1
        taken.add(identifier)
        effects = []
        if index and element.effects:
            counter += 1
            effects.append(_replace_effect_id(element.effects[0], f"fx-{counter}"))
        migrated.append(_replace(element, id=identifier, effects=tuple(effects)))
    return add_edge_fades(migrated, fades)


def _replace_effect_id(effect: Effect, identifier: str) -> Effect:
    return Effect(identifier, effect.type, effect.offset, effect.duration, effect.origin)


# --- validation of the structure ---------------------------------------------------------------------


def _reject_unknown(data: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise PlanError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}")


def _number(value: object, where: str, *, minimum: float | None = None, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanError(f"{where} must be a number")
    if minimum is not None and (value <= minimum if exclusive else value < minimum):
        relation = "greater than" if exclusive else "at least"
        raise PlanError(f"{where} must be {relation} {minimum:g}")
    return float(value)


def _optional_number(data: dict, key: str, where: str, *, minimum: float | None = None) -> float | None:
    return None if data.get(key) is None else _number(data[key], f"{where}: '{key}'", minimum=minimum)


def _output(data: object) -> OutputSpec:
    if not isinstance(data, dict):
        raise PlanError("'output' must be an object with width, height and fps")
    _reject_unknown(data, {"width", "height", "fps"}, "'output'")
    sizes = []
    for key in ("width", "height"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 2 or value % 2:
            raise PlanError(f"'output.{key}' must be an even integer of at least 2 (required by yuv420p)")
        sizes.append(value)
    fps = _number(data.get("fps", 30), "'output.fps'", minimum=0, exclusive=True)
    if fps > MAX_FPS:
        raise PlanError(f"'output.fps' must be at most {MAX_FPS}")
    return OutputSpec(sizes[0], sizes[1], fps)


def _relative_path(value: object, where: str, key: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanError(f"{where}: '{key}' must be a non-empty path relative to {what}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise PlanError(f"{where}: '{key}' must be a relative path inside {what}, got {value!r}")
    return value


def _source(value: object, where: str) -> str:
    return _relative_path(value, where, "source", "the input folder")


def _origin(value: object, where: str, key: str = "origin") -> str:
    if value not in ORIGINS:
        raise PlanError(f"{where}: '{key}' must be one of {', '.join(ORIGINS)}, got {value!r}")
    return value


def _transition(data: object, where: str) -> TransitionRequest:
    if data is None:
        return TransitionRequest()
    if not isinstance(data, dict):
        raise PlanError(f"{where}: 'transition_in' must be an object with type and duration")
    _reject_unknown(data, {"type", "duration"}, f"{where}: 'transition_in'")
    kind = data.get("type", CUT)
    if kind not in TRANSITION_TYPES:
        raise PlanError(f"{where}: 'transition_in.type' must be one of {', '.join(TRANSITION_TYPES)}, got {kind!r}")
    duration = _number(data.get("duration", 0), f"{where}: 'transition_in.duration'", minimum=0)
    if kind == DISSOLVE and duration <= 0:
        raise PlanError(f"{where}: a dissolve needs a 'transition_in.duration' greater than 0")
    return TransitionRequest(kind, 0.0 if kind == CUT else duration)


def _effects(data: object, where: str) -> tuple[Effect, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise PlanError(f"{where}: 'effects' must be a list")
    effects = []
    for number, item in enumerate(data):
        here = f"{where}: effects[{number}]"
        if not isinstance(item, dict):
            raise PlanError(f"{here} must be an object")
        _reject_unknown(item, {"id", "type", "offset", "duration", "origin"}, here)
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise PlanError(f"{here}: 'id' must be a non-empty string")
        if item.get("type") not in EFFECT_TYPES:
            raise PlanError(f"{here}: 'type' must be one of {', '.join(EFFECT_TYPES)}, got {item.get('type')!r}")
        effects.append(Effect(
            identifier, item["type"],
            _number(item.get("offset", 0), f"{here}: 'offset'", minimum=0),
            _number(item.get("duration"), f"{here}: 'duration'", minimum=0, exclusive=True),
            _origin(item.get("origin", PLANNER), here),
        ))
    if sum(1 for effect in effects if effect.type == DISSOLVE) > 1:
        raise PlanError(f"{where}: a clip has at most one dissolve (the one into it)")
    return tuple(effects)


def _element(data: object, where: str, output: OutputSpec, legacy: bool) -> PlanElement:
    if not isinstance(data, dict):
        raise PlanError(f"{where} must be an object")
    _reject_unknown(data, _LEGACY_ELEMENT_KEYS if legacy else _ELEMENT_KEYS, where)
    kind = data.get("type")
    if kind not in (PHOTO, VIDEO):
        raise PlanError(f"{where}: 'type' must be 'photo' or 'video', got {kind!r}")

    start = end = None
    if kind == VIDEO:
        for key in ("start", "end"):
            if data.get(key) is None:
                raise PlanError(f"{where}: a video needs '{key}'")
        start = _number(data["start"], f"{where}: 'start'", minimum=0)
        end = _number(data["end"], f"{where}: 'end'", minimum=start, exclusive=True)
        duration = end - start
        if data.get("duration") is not None:
            declared = _number(data["duration"], f"{where}: 'duration'", minimum=0, exclusive=True)
            if abs(declared - duration) > 1.5 / output.fps:
                raise PlanError(
                    f"{where}: 'duration' ({declared:g}) does not match end - start ({duration:g}); "
                    "edit 'start' and 'end', or remove 'duration'"
                )
        if data.get("duration_origin") is not None:
            raise PlanError(f"{where}: a video has no 'duration_origin': its duration is end - start")
    else:
        if data.get("start") is not None or data.get("end") is not None:
            raise PlanError(f"{where}: a photo has no 'start' or 'end', only a 'duration'")
        if data.get("duration") is None:
            raise PlanError(f"{where}: a photo needs a 'duration'")
        duration = _number(data["duration"], f"{where}: 'duration'", minimum=0, exclusive=True)

    texts = {}
    for key in DESCRIPTIVE_KEYS:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise PlanError(f"{where}: '{key}' must be a string")
        texts[key] = value

    extra: dict = {}
    if legacy:
        transition = _transition(data.get("transition_in"), where)
        if transition.type == DISSOLVE:
            extra["effects"] = (Effect("", DISSOLVE, 0.0, transition.duration, PLANNER),)
    else:
        identifier = data.get("id")
        if identifier is not None and (not isinstance(identifier, str) or not identifier):
            raise PlanError(f"{where}: 'id' must be a non-empty string")
        extra["id"] = identifier or ""
        extra["origin"] = _origin(data.get("origin", PLANNER), where)
        if kind == PHOTO and data.get("duration_origin") is not None:
            extra["duration_origin"] = _origin(data["duration_origin"], where, "duration_origin")
        extra["effects"] = _effects(data.get("effects"), where)
    return PlanElement(
        source=_source(data.get("source"), where),
        kind=kind,
        duration=duration,
        start=start,
        end=end,
        score=_score(data.get("score"), where),
        timestamp=_optional_number(data, "timestamp", where, minimum=0),
        **texts,
        **extra,
    )


def _check_unique_ids(elements: list[PlanElement]) -> None:
    seen: dict[str, int] = {}
    effects: dict[str, str] = {}
    for index, element in enumerate(elements):
        if element.id in seen:
            raise PlanError(f"elements[{index}]: 'id' {element.id!r} is already used by elements[{seen[element.id]}]")
        seen[element.id] = index
        for effect in element.effects:
            if effect.id in effects:
                raise PlanError(f"elements[{index}]: effect id {effect.id!r} is already used in {effects[effect.id]}")
            effects[effect.id] = f"elements[{index}]"


def _exclusions(data: object) -> tuple[Exclusion, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise PlanError("'excluded' must be a list")
    result = []
    for number, item in enumerate(data):
        where = f"excluded[{number}]"
        if not isinstance(item, dict):
            raise PlanError(f"{where} must be an object")
        _reject_unknown(item, {"source", "start", "end", "reason"}, where)
        reason = item.get("reason", EXCLUDED_BY_USER)
        if reason != EXCLUDED_BY_USER:
            raise PlanError(f"{where}: 'reason' must be {EXCLUDED_BY_USER!r}, got {reason!r}")
        start, end = item.get("start"), item.get("end")
        if (start is None) != (end is None):
            raise PlanError(f"{where}: 'start' and 'end' are both given (a moment) or both null (a photo)")
        if start is not None:
            start = _number(start, f"{where}: 'start'", minimum=0)
            end = _number(end, f"{where}: 'end'", minimum=start, exclusive=True)
        result.append(Exclusion(_relative_path(item.get("source"), where, "source", "the input folder"), start, end))
    return tuple(result)


def _music(data: object) -> MusicPlan | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise PlanError("'music' must be an object with tracks and offset")
    _reject_unknown(data, {"tracks", "offset"}, "'music'")
    tracks_data = data.get("tracks", [])
    if not isinstance(tracks_data, list):
        raise PlanError("'music.tracks' must be a list")
    tracks = []
    for number, item in enumerate(tracks_data):
        where = f"music.tracks[{number}]"
        if not isinstance(item, dict):
            raise PlanError(f"{where} must be an object")
        _reject_unknown(item, {"file", "parts"}, where)
        parts_data = item.get("parts")
        if not isinstance(parts_data, list) or not parts_data:
            raise PlanError(f"{where}: 'parts' must be a non-empty list")
        tracks.append(MusicTrack(
            _relative_path(item.get("file"), where, "file", "the music folder"),
            tuple(_music_part(part, f"{where}.parts[{index}]") for index, part in enumerate(parts_data)),
        ))
    offset = _number(data.get("offset", 0), "'music.offset'")
    return MusicPlan(tuple(tracks), offset)


def _music_part(data: object, where: str) -> MusicPart:
    if not isinstance(data, dict):
        raise PlanError(f"{where} must be an object")
    _reject_unknown(data, {"start", "end", "muted", "fade_in", "fade_out"}, where)
    for key in ("start", "end"):
        if data.get(key) is None:
            raise PlanError(f"{where}: a part needs '{key}'")
    start = _number(data["start"], f"{where}: 'start'", minimum=0)
    end = _number(data["end"], f"{where}: 'end'", minimum=start, exclusive=True)
    muted = data.get("muted", False)
    if not isinstance(muted, bool):
        raise PlanError(f"{where}: 'muted' must be true or false")
    return MusicPart(
        start, end, muted,
        _number(data.get("fade_in", 0), f"{where}: 'fade_in'", minimum=0),
        _number(data.get("fade_out", 0), f"{where}: 'fade_out'", minimum=0),
    )


def _score(data: object, where: str) -> dict | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise PlanError(f"{where}: 'score' must be an object")
    _reject_unknown(data, set(SCORE_KEYS), f"{where}: 'score'")
    for key in ("final", "technical_score", "narrative_score"):
        if data.get(key) is not None:
            _number(data[key], f"{where}: 'score.{key}'")
    if data.get("components") is not None and not isinstance(data["components"], dict):
        raise PlanError(f"{where}: 'score.components' must be an object")
    return data
