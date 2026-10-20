"""`edit_plan.json`: the hand-editable plan the renderer works from.

Times of a clip (`start`, `end`) are seconds from its first frame, that is relative to the stream
as FFprobe exposes it (a container whose first timestamp is not zero does not shift them).
`source` is the path relative to `--input`.

Every element carries what the planner knows (scores, moment type, category, section, description,
reason), but the renderer needs only `source`, `type`, the extent and `transition_in`. A
transition written by hand is not trusted: the renderer reduces it to the limits of section 8.
Unknown keys are rejected, so a typo in a hand-edited plan is an error and not a silent no-op.
"""

import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path, PurePosixPath

from vacation_memories.atomic import atomic_write_json
from vacation_memories.config import ConfigError
from vacation_memories.transitions import (
    CUT,
    DISSOLVE,
    TRANSITION_TYPES,
    Layout,
    TransitionRequest,
    TransitionRules,
    layout_timeline,
    seconds_to_frames,
)

SCHEMA_VERSION = 1
PHOTO = "photo"
VIDEO = "video"
MAX_FPS = 240
NTSC_NUMERATORS = (24000, 30000, 48000, 60000, 120000)
NTSC_TOLERANCE = 0.005
SCORE_KEYS = ("final", "technical_score", "narrative_score", "components")
DESCRIPTIVE_KEYS = ("moment_type", "category", "section", "description", "reason")
_ELEMENT_KEYS = {
    "source", "type", "start", "end", "duration", "score", "transition_in", "timestamp", *DESCRIPTIVE_KEYS,
}
_PLAN_KEYS = {
    "schema_version", "requested_duration", "planned_duration", "output", "assumed_tz", "runtime", "elements",
}


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
class PlanElement:
    source: str
    kind: str  # "photo" or "video"
    duration: float
    start: float | None = None  # clips only
    end: float | None = None
    transition_in: TransitionRequest = field(default_factory=TransitionRequest)
    score: dict | None = None
    moment_type: str | None = None
    category: str | None = None
    section: str | None = None
    description: str | None = None
    reason: str | None = None
    timestamp: float | None = None  # destination, computed by `finalize_timeline`

    def to_dict(self) -> dict:
        data: dict = {"source": self.source, "type": self.kind}
        if self.kind == VIDEO:
            data["start"] = self.start
            data["end"] = self.end
        data["duration"] = self.duration
        for key in DESCRIPTIVE_KEYS:
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        if self.score is not None:
            data["score"] = self.score
        data["transition_in"] = {"type": self.transition_in.type, "duration": self.transition_in.duration}
        if self.timestamp is not None:
            data["timestamp"] = self.timestamp
        return data


@dataclass(frozen=True)
class EditPlan:
    output: OutputSpec
    elements: list[PlanElement]
    requested_duration: float | None = None
    planned_duration: float | None = None
    assumed_tz: str | None = None
    runtime: dict | None = None
    schema_version: int = SCHEMA_VERSION

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
        return data


def save_plan(plan: EditPlan, path: Path) -> None:
    atomic_write_json(path, plan.to_dict())


def load_plan(path: Path) -> EditPlan:
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
        return plan_from_dict(data)
    except PlanError as error:
        raise PlanError(f"invalid plan {path}: {error}") from None


def plan_from_dict(data: object) -> EditPlan:
    if not isinstance(data, dict):
        raise PlanError("the plan must be a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PlanError(f"unsupported schema_version {version!r}: this version supports {SCHEMA_VERSION}")
    _reject_unknown(data, _PLAN_KEYS, "the plan")

    output = _output(data.get("output"))
    elements_data = data.get("elements")
    if not isinstance(elements_data, list):
        raise PlanError("'elements' must be a list")
    elements = [_element(item, f"elements[{index}]", output) for index, item in enumerate(elements_data)]

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
    )


def finalize_timeline(plan: EditPlan, rules: TransitionRules | None = None) -> tuple[EditPlan, Layout]:
    """Apply the transition limits and fill in the destination timestamps and `planned_duration`.

    This is what the planner calls before saving; the renderer calls the same layout function.
    """
    rules = rules or TransitionRules()
    layout = plan_layout(plan, rules)
    elements = []
    for element, placement in zip(plan.elements, layout.placements):
        transition = TransitionRequest(placement.effective_type, layout.seconds(placement.dissolve_frames))
        elements.append(_replace(element, transition_in=transition, timestamp=layout.seconds(placement.start_frame)))
    planned = round(layout.duration, 6) if elements else 0.0
    return _replace_plan(plan, elements=elements, planned_duration=planned), layout


def plan_layout(plan: EditPlan, rules: TransitionRules) -> Layout:
    fps = plan.output.frame_rate
    frames = [max(1, seconds_to_frames(element.duration, fps)) for element in plan.elements]
    return layout_timeline(frames, [element.transition_in for element in plan.elements], fps, rules)


def _replace(element: PlanElement, **changes) -> PlanElement:
    return PlanElement(**{**element.__dict__, **changes})


def _replace_plan(plan: EditPlan, **changes) -> EditPlan:
    return EditPlan(**{**plan.__dict__, **changes})


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


def _source(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanError(f"{where}: 'source' must be a non-empty path relative to the input folder")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise PlanError(f"{where}: 'source' must be a relative path inside the input folder, got {value!r}")
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


def _element(data: object, where: str, output: OutputSpec) -> PlanElement:
    if not isinstance(data, dict):
        raise PlanError(f"{where} must be an object")
    _reject_unknown(data, _ELEMENT_KEYS, where)
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
    return PlanElement(
        source=_source(data.get("source"), where),
        kind=kind,
        duration=duration,
        start=start,
        end=end,
        transition_in=_transition(data.get("transition_in"), where),
        score=_score(data.get("score"), where),
        timestamp=_optional_number(data, "timestamp", where, minimum=0),
        **texts,
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
