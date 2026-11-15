"""What the renderer does with the effects of the clips: fades and dips inside the limits, with notes."""

from fractions import Fraction

import pytest

from vacation_memories.planning.edit_plan import DIP, FADE_IN, FADE_OUT, VIDEO, Effect, PlanElement
from vacation_memories.planning.transitions import (
    DISSOLVE,
    PLANNER,
    USER,
    TransitionRules,
    layout_timeline,
)
from vacation_memories.rendering.effects import music_fades, resolve_effects
from vacation_memories.rendering.render_filters import ClipFade

FPS = Fraction(30)
RULES = TransitionRules()


def clip(seconds: float, *effects: Effect) -> PlanElement:
    return PlanElement(source="a.mp4", kind=VIDEO, duration=seconds, start=0.0, end=seconds, effects=effects)


def effect(kind: str, offset: float, duration: float, origin: str = USER, identifier: str | None = None) -> Effect:
    return Effect(identifier or f"{kind}-{offset}", kind, offset, duration, origin)


def resolve(*elements: PlanElement):
    frames = [round(element.duration * 30) for element in elements]
    layout = layout_timeline(frames, [element.transition_in for element in elements], FPS, RULES)
    return resolve_effects(list(elements), layout, RULES), layout


def test_fades_at_the_edges_become_frame_exact_fades():
    result, _ = resolve(clip(4.0, effect(FADE_IN, 0.0, 1.0), effect(FADE_OUT, 2.5, 1.5)))
    assert result.fades == [(ClipFade(FADE_IN, 0, 30), ClipFade(FADE_OUT, 75, 45))]
    assert result.adjusted == []


def test_a_clip_without_effects_has_no_fades():
    result, _ = resolve(clip(2.0), clip(2.0))
    assert result.fades == [(), ()] and result.adjusted == []


def test_a_fade_longer_than_its_clip_is_reduced_to_the_clip():
    result, _ = resolve(clip(2.0, effect(FADE_IN, 0.0, 5.0)))
    assert result.fades == [(ClipFade(FADE_IN, 0, 60),)]
    [note] = result.adjusted
    assert note.type == FADE_IN and note.effective_duration == pytest.approx(2.0)
    assert "reduced" in note.reason


def test_a_fade_in_that_does_not_start_at_the_clip_start_snaps_to_it():
    result, _ = resolve(clip(4.0, effect(FADE_IN, 1.0, 1.0)))
    assert result.fades == [(ClipFade(FADE_IN, 0, 30),)]
    assert "start of the clip" in result.adjusted[0].reason


def test_a_fade_out_that_does_not_end_at_the_clip_end_snaps_to_it():
    result, _ = resolve(clip(4.0, effect(FADE_OUT, 0.5, 1.0)))
    assert result.fades == [(ClipFade(FADE_OUT, 90, 30),)]
    assert "end of the clip" in result.adjusted[0].reason


def test_a_fade_out_on_a_clip_that_ends_in_a_dissolve_is_dropped():
    dissolve = (effect(DISSOLVE, 0.0, 1.0, PLANNER, "d"),)
    result, layout = resolve(clip(4.0, effect(FADE_OUT, 2.5, 1.5)), clip(4.0, *dissolve))
    assert layout.placements[1].dissolve_frames == 30  # the last second of the clip is the dissolve's
    assert result.fades[0] == ()
    assert "overlaps" in result.adjusted[0].reason


def test_a_fade_out_shrinks_to_the_room_a_long_fade_in_leaves():
    result, _ = resolve(clip(4.0, effect(FADE_IN, 0.0, 3.0), effect(FADE_OUT, 2.0, 2.0)))
    assert result.fades == [(ClipFade(FADE_IN, 0, 90), ClipFade(FADE_OUT, 90, 30))]
    assert result.adjusted[0].effective_duration == pytest.approx(1.0)


def test_a_fade_in_on_a_clip_that_starts_with_a_dissolve_is_dropped():
    dissolve = (effect(DISSOLVE, 0.0, 1.0, PLANNER, "d"),)
    result, _ = resolve(clip(4.0), clip(4.0, effect(FADE_IN, 0.0, 1.0), *dissolve))
    assert result.fades[1] == ()
    [note] = result.adjusted
    assert note.effective_duration is None and "overlaps" in note.reason


def test_an_effect_below_the_minimum_is_dropped_with_a_note():
    result, _ = resolve(clip(4.0, effect(FADE_IN, 0.0, 0.1)))
    assert result.fades == [()]
    assert "minimum" in result.adjusted[0].reason and result.adjusted[0].effective_duration is None


def test_a_dip_can_sit_in_the_middle_of_a_clip():
    result, _ = resolve(clip(4.0, effect(DIP, 1.5, 1.0)))
    assert result.fades == [(ClipFade(DIP, 45, 30),)] and result.adjusted == []


def test_a_dip_is_trimmed_to_the_free_space_between_other_effects():
    effects = (effect(FADE_IN, 0.0, 1.0), effect(DIP, 0.5, 1.5))  # the dip starts inside the fade
    result, _ = resolve(clip(4.0, *effects))
    assert result.fades == [(ClipFade(FADE_IN, 0, 30), ClipFade(DIP, 30, 30))]
    assert "reduced" in result.adjusted[0].reason


def test_a_dip_inside_another_effect_is_dropped():
    effects = (effect(FADE_OUT, 2.0, 2.0), effect(DIP, 2.5, 0.5))
    result, _ = resolve(clip(4.0, *effects))
    assert result.fades == [(ClipFade(FADE_OUT, 60, 60),)]
    assert result.adjusted[0].type == DIP and result.adjusted[0].effective_duration is None


def test_a_dip_past_the_end_of_the_clip_is_cut_at_the_end():
    result, _ = resolve(clip(2.0, effect(DIP, 1.5, 1.0)))
    assert result.fades == [(ClipFade(DIP, 45, 15),)]


def test_the_notes_name_the_effect_and_what_it_asked_for():
    result, _ = resolve(clip(2.0, effect(FADE_IN, 0.0, 5.0, identifier="fx-9")))
    [note] = result.adjusted
    assert note.id == "fx-9" and note.requested_duration == 5.0 and note.requested_offset == 0.0
    assert note.to_dict()["effective"] == {"offset": 0.0, "duration": 2.0}


def test_applied_effects_are_listed_with_their_effective_extent():
    result, _ = resolve(clip(4.0, effect(FADE_IN, 0.0, 1.0, identifier="fx-1")))
    assert result.applied == [{"id": "fx-1", "type": FADE_IN, "offset": 0.0, "duration": 1.0}]


class TestMusicFades:
    """The music follows the video's first fade in and last fade out; without them, the configuration."""

    def test_the_fade_of_the_edge_clips_sets_them(self):
        result, layout = resolve(clip(4.0, effect(FADE_IN, 0.0, 0.5)), clip(4.0, effect(FADE_OUT, 2.5, 1.5)))
        assert music_fades(result, layout, default_in=1.0, default_out=3.0) == (0.5, 1.5)

    def test_without_effects_the_defaults_apply(self):
        result, layout = resolve(clip(4.0), clip(4.0))
        assert music_fades(result, layout, default_in=1.0, default_out=3.0) == (1.0, 3.0)

    def test_a_fade_in_in_the_middle_of_the_timeline_does_not_count(self):
        result, layout = resolve(clip(4.0), clip(4.0, effect(FADE_IN, 0.0, 0.5)))
        assert music_fades(result, layout, default_in=1.0, default_out=3.0) == (1.0, 3.0)
