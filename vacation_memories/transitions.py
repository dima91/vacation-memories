"""Transitions between clips: effective dissolve lengths and the timeline layout.

`layout_timeline` is the single function that turns clip lengths and requested transitions into
effective dissolves, destination timestamps and the total duration. The planner (to write the plan)
and the renderer (to build the video) both call it, so plan and video cannot drift apart. Everything
is computed in whole frames and converted to seconds only at the end.

Rules (section 8 of the specification):
1. `d_eff = min(d, max_fraction * t_min)`, where `t_min` is the shorter of the two clips, so the
   dissolves in and out of a clip never take more than twice `max_fraction` of it;
2. `d_eff` is rounded down to a whole number of frames;
3. if `d_eff` is shorter than `min_seconds`, the dissolve becomes a `cut`.
"""

import math
from dataclasses import dataclass
from fractions import Fraction

CUT = "cut"
DISSOLVE = "dissolve"
TRANSITION_TYPES = (CUT, DISSOLVE)

DEFAULT_DISSOLVE_SECONDS = 0.8
MAX_DISSOLVE_FRACTION = 0.25
MIN_DISSOLVE_SECONDS = 0.2
_EPSILON = 1e-6


@dataclass(frozen=True)
class TransitionRules:
    """Limits applied to every dissolve, whoever wrote it."""

    default_seconds: float = DEFAULT_DISSOLVE_SECONDS
    max_fraction: float = MAX_DISSOLVE_FRACTION
    min_seconds: float = MIN_DISSOLVE_SECONDS


@dataclass(frozen=True)
class TransitionRequest:
    """What the plan asks for between a clip and the previous one."""

    type: str = CUT
    duration: float = 0.0


@dataclass(frozen=True)
class Placement:
    """One clip on the timeline. `dissolve_frames` is the effective incoming dissolve (0 = cut)."""

    index: int
    start_frame: int
    frames: int
    dissolve_frames: int
    requested: TransitionRequest
    note: str | None  # why the effective transition differs from the request, if it does

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.frames

    @property
    def effective_type(self) -> str:
        return DISSOLVE if self.dissolve_frames else CUT


@dataclass(frozen=True)
class Layout:
    fps: Fraction
    placements: list[Placement]

    @property
    def total_frames(self) -> int:
        return self.placements[-1].end_frame if self.placements else 0

    @property
    def duration(self) -> float:
        return float(self.total_frames / self.fps)

    def seconds(self, frames: int) -> float:
        return float(frames / self.fps)

    def adjusted(self) -> list[Placement]:
        """Placements whose transition differs from what was requested."""
        return [placement for placement in self.placements if placement.note]


def seconds_to_frames(seconds: float, fps: Fraction) -> int:
    """Nearest whole number of frames."""
    return round(seconds * fps)


def effective_dissolve_frames(
    request: TransitionRequest, left_frames: int, right_frames: int, fps: Fraction, rules: TransitionRules
) -> tuple[int, str | None]:
    """Frames of the dissolve between two clips, and a note when it differs from the request."""
    if request.type == CUT or request.duration <= 0:
        return 0, None
    shorter = min(left_frames, right_frames) / fps
    allowed = min(request.duration, rules.max_fraction * float(shorter))
    frames = math.floor(allowed * fps + _EPSILON)
    if frames / fps < rules.min_seconds - _EPSILON:
        return 0, (
            f"dissolve of {request.duration:g} s turned into a cut "
            f"(the limit for these clips is {allowed:.2f} s, below the {rules.min_seconds:g} s minimum)"
        )
    effective = float(frames / fps)
    if effective < request.duration - float(1 / fps) - _EPSILON:
        return frames, f"dissolve reduced from {request.duration:g} s to {effective:.2f} s"
    return frames, None


def layout_timeline(
    clip_frames: list[int],
    requests: list[TransitionRequest],
    fps: Fraction,
    rules: TransitionRules,
    forced_cuts: frozenset[int] = frozenset(),
) -> Layout:
    """Place the clips on the timeline.

    `requests[i]` is the transition into clip `i` (the first one has none). `forced_cuts` are
    indexes whose incoming transition must be a cut, whatever was requested (block boundaries).
    """
    if len(clip_frames) != len(requests):
        raise ValueError("one transition request per clip is needed")
    placements: list[Placement] = []
    start = 0
    for index, (frames, request) in enumerate(zip(clip_frames, requests)):
        if frames < 1:
            raise ValueError(f"clip {index} has no frames")
        dissolve, note = 0, None
        if index and index in forced_cuts:
            if request.type == DISSOLVE and request.duration > 0:
                note = "dissolve turned into a cut at a block boundary"
        elif index:
            dissolve, note = effective_dissolve_frames(request, clip_frames[index - 1], frames, fps, rules)
        start = start - dissolve if index else 0
        placements.append(Placement(index, start, frames, dissolve, request, note))
        start += frames
    return Layout(fps, placements)


def split_blocks(layout: Layout, block_size: int) -> tuple[list[range], frozenset[int]]:
    """Group consecutive clips into blocks of at most `block_size`, split at hard cuts.

    A block ends at the last cut that fits; when a whole window is joined by dissolves, the
    dissolve at the limit becomes a cut. Returns the index ranges and the forced cuts.
    """
    if block_size < 1:
        raise ValueError("block_size must be at least 1")
    count = len(layout.placements)
    blocks: list[range] = []
    forced: set[int] = set()
    first = 0
    while first < count:
        limit = min(first + block_size, count)
        if limit == count:
            blocks.append(range(first, count))
            break
        cut = next((i for i in range(limit, first, -1) if layout.placements[i].dissolve_frames == 0), None)
        if cut is None:
            cut = limit
            forced.add(cut)
        blocks.append(range(first, cut))
        first = cut
    return blocks, frozenset(forced)
