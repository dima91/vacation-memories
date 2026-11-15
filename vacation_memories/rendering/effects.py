"""The effects of the clips (`fade_in`, `fade_out`, `dip`), brought inside the limits of the timeline.

The dissolves are laid out by `transitions.layout_timeline`, the shared function. What is left are the
fades to and from black, which the editor and a hand-edited plan may put on any clip. They are applied
as written when they fit, and otherwise reduced, with a note that the renderer logs and writes into the
report (decision D3 of `UI-SPEC.md`):

- a `fade_in` starts at the start of the clip and a `fade_out` ends at its end (D1: an effect that is
  elsewhere snaps to the edge), a `dip` can be anywhere;
- no effect leaves the clip or overlaps another effect of it, or the dissolve into or out of the clip,
  which the layout has already fixed;
- an effect shorter than the minimum duration (0.2 s) is left out.

All of it is in whole frames, like the layout, so the fades do not depend on timestamps.
"""

import math
from dataclasses import dataclass, field, replace
from fractions import Fraction

from vacation_memories.planning.edit_plan import DIP, FADE_IN, FADE_OUT, PlanElement
from vacation_memories.planning.transitions import DISSOLVE, Layout, TransitionRules
from vacation_memories.rendering.render_filters import ClipFade

_EPSILON = 1e-6
_ORDER = {FADE_IN: 0, FADE_OUT: 1, DIP: 2}  # the edges first: a dip gives way to them

Interval = tuple[int, int]


@dataclass(frozen=True)
class EffectNote:
    """An effect that was reduced or left out, with what it asked for and what it got (`None`: left out)."""

    id: str
    type: str
    requested_offset: float
    requested_duration: float
    effective_offset: float | None
    effective_duration: float | None
    reason: str
    source: str = ""  # the file of the clip the effect is on

    def to_dict(self) -> dict:
        effective = None
        if self.effective_duration is not None:
            effective = {"offset": self.effective_offset, "duration": self.effective_duration}
        return {
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "requested": {"offset": self.requested_offset, "duration": self.requested_duration},
            "effective": effective,
            "reason": self.reason,
        }

    def __str__(self) -> str:
        return f"{self.source}: {self.type} effect {self.id!r}: {self.reason}"


@dataclass
class EffectsResult:
    fades: list[tuple[ClipFade, ...]]  # for every clip of the layout, its fades in order
    applied: list[dict] = field(default_factory=list)
    adjusted: list[EffectNote] = field(default_factory=list)


def resolve_effects(elements: list[PlanElement], layout: Layout, rules: TransitionRules) -> EffectsResult:
    """The fades of every clip of `layout` (`elements` are the clips that were rendered, in order)."""
    fps = layout.fps
    result = EffectsResult([])
    for index, (element, placement) in enumerate(zip(elements, layout.placements)):
        frames = placement.frames
        taken: list[Interval] = []
        if placement.dissolve_frames:
            taken.append((0, placement.dissolve_frames))
        following = layout.placements[index + 1].dissolve_frames if index + 1 < len(elements) else 0
        if following:
            taken.append((frames - following, frames))
        fades = []
        wanted = sorted((effect for effect in element.effects if effect.type != DISSOLVE),
                        key=lambda effect: (_ORDER[effect.type], effect.offset))
        for effect in wanted:
            fade, note = _place(effect, frames, taken, fps, rules)
            if note:
                result.adjusted.append(replace(note, source=element.source))
            if fade is None:
                continue
            taken.append((fade.start, fade.start + fade.frames))
            fades.append(fade)
            result.applied.append({
                "id": effect.id, "type": effect.type,
                "offset": round(float(Fraction(fade.start) / fps), 6),
                "duration": round(float(Fraction(fade.frames) / fps), 6),
            })
        result.fades.append(tuple(sorted(fades, key=lambda fade: fade.start)))
    return result


def _place(effect, frames: int, taken: list[Interval], fps: Fraction, rules: TransitionRules):
    """The fade an effect becomes in a clip of `frames` frames, and a note if it had to change."""
    wanted = math.floor(effect.duration * fps + _EPSILON)
    notes: list[str] = []
    if effect.type == FADE_IN:
        start = 0
        room = min((first for first, _ in taken), default=frames)
        if abs(effect.offset) * fps >= 1:
            notes.append("moved to the start of the clip")
        end = min(wanted, room)
    elif effect.type == FADE_OUT:
        room = max((last for _, last in taken), default=0)
        if abs(effect.offset + effect.duration - frames / fps) * fps >= 1:
            notes.append("moved to the end of the clip")
        start = max(frames - wanted, room)
        end = frames
    else:
        start = min(max(round(effect.offset * fps), 0), frames)
        end = min(start + wanted, frames)
        start, end = _free_part(start, end, _gaps(taken, frames))

    granted = end - start
    if granted < wanted:
        notes.append("overlaps another effect or a dissolve" if granted <= 0 else "reduced to the free space of the clip")
    if granted / fps < rules.min_seconds - _EPSILON:
        notes.append(f"left out: below the minimum of {rules.min_seconds:g} s")
        granted = 0
    if granted <= 0:
        return None, _note(effect, None, None, notes, fps)
    note = _note(effect, start, granted, notes, fps) if notes else None
    return ClipFade(effect.type, start, granted), note


def _gaps(taken: list[Interval], length: int) -> list[Interval]:
    gaps, cursor = [], 0
    for start, end in sorted(taken):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if length > cursor:
        gaps.append((cursor, length))
    return gaps


def _free_part(start: int, end: int, gaps: list[Interval]) -> Interval:
    """The part of [start, end) in the free gap it overlaps most; empty when it overlaps none."""
    best, best_size = (start, start), 0
    for first, last in gaps:
        low, high = max(start, first), min(end, last)
        if high - low > best_size:
            best, best_size = (low, high), high - low
    return best


def _note(effect, start: int | None, frames: int | None, reasons: list[str], fps: Fraction) -> EffectNote:
    return EffectNote(
        id=effect.id,
        type=effect.type,
        requested_offset=effect.offset,
        requested_duration=effect.duration,
        effective_offset=None if start is None else round(float(Fraction(start) / fps), 6),
        effective_duration=None if frames is None else round(float(Fraction(frames) / fps), 6),
        reason="; ".join(reasons),
    )


def music_fades(
    result: EffectsResult, layout: Layout, *, default_in: float, default_out: float
) -> tuple[float, float]:
    """Seconds of the music's fade in and fade out: those of the video's first and last clip, else the defaults.

    The music follows the picture's fade from black at the start and to black at the end; a video
    without them still gets the configured fades (`fade_in_seconds`, `fade_out_seconds`).
    """
    if not result.fades:
        return default_in, default_out
    fade_in = next((fade for fade in result.fades[0] if fade.kind == FADE_IN), None)
    fade_out = next((fade for fade in result.fades[-1] if fade.kind == FADE_OUT), None)
    return (
        default_in if fade_in is None else layout.seconds(fade_in.frames),
        default_out if fade_out is None else layout.seconds(fade_out.frames),
    )
