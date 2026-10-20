from fractions import Fraction

import pytest

from vacation_memories.transitions import (
    CUT,
    DISSOLVE,
    TransitionRequest,
    TransitionRules,
    effective_dissolve_frames,
    layout_timeline,
    seconds_to_frames,
    split_blocks,
)

FPS = Fraction(30)
RULES = TransitionRules()


def dissolve(seconds: float) -> TransitionRequest:
    return TransitionRequest(DISSOLVE, seconds)


def effective(seconds: float, left: float, right: float, fps: Fraction = FPS):
    frames, note = effective_dissolve_frames(
        dissolve(seconds), seconds_to_frames(left, fps), seconds_to_frames(right, fps), fps, RULES
    )
    return frames / fps, note


def test_long_clips_keep_the_requested_dissolve():
    seconds, note = effective(0.8, 5.0, 6.0)
    assert seconds == pytest.approx(0.8)
    assert note is None


def test_dissolve_is_limited_to_a_quarter_of_the_shorter_clip():
    seconds, note = effective(0.8, 1.2, 10.0)
    assert seconds == pytest.approx(0.3)  # 25% of 1.2 s, already a whole number of frames
    assert "reduced from 0.8 s to 0.30 s" in note


def test_the_shorter_of_the_two_clips_decides():
    assert effective(0.8, 10.0, 1.2)[0] == effective(0.8, 1.2, 10.0)[0]


def test_dissolve_is_rounded_down_to_whole_frames():
    frames, _ = effective_dissolve_frames(dissolve(0.8), 50, 500, Fraction(24), RULES)
    assert frames == 12  # 25% of 50/24 s = 0.52 s = 12.5 frames, rounded down


def test_dissolve_below_the_minimum_becomes_a_cut():
    frames, note = effective_dissolve_frames(dissolve(0.8), 21, 300, FPS, RULES)  # 25% of 0.7 s = 0.175 s
    assert frames == 0
    assert "turned into a cut" in note


def test_minimum_threshold_is_configurable():
    rules = TransitionRules(min_seconds=0.1)
    frames, _ = effective_dissolve_frames(dissolve(0.8), 21, 300, FPS, rules)
    assert frames == 5  # 0.175 s = 5.25 frames


def test_rounding_to_frames_alone_is_not_reported_as_a_reduction():
    _, note = effective_dissolve_frames(dissolve(0.8), 300, 300, Fraction(24), RULES)
    assert note is None  # 0.8 s = 19.2 frames -> 19 frames


@pytest.mark.parametrize("request_", [TransitionRequest(CUT, 0.0), TransitionRequest(DISSOLVE, 0.0)])
def test_a_cut_stays_a_cut(request_):
    assert effective_dissolve_frames(request_, 90, 90, FPS, RULES) == (0, None)


def test_layout_duration_is_the_sum_of_clips_minus_the_dissolves():
    layout = layout_timeline([120, 120, 60], [TransitionRequest(), dissolve(0.8), dissolve(0.5)], FPS, RULES)
    assert [p.dissolve_frames for p in layout.placements] == [0, 24, 15]
    assert layout.total_frames == 120 + 120 + 60 - 24 - 15
    assert layout.duration == pytest.approx((300 - 39) / 30)


def test_layout_gives_the_destination_timestamps():
    layout = layout_timeline([120, 120, 60], [TransitionRequest(), dissolve(0.8), TransitionRequest()], FPS, RULES)
    assert [p.start_frame for p in layout.placements] == [0, 120 - 24, 120 - 24 + 120]
    assert [layout.seconds(p.start_frame) for p in layout.placements] == pytest.approx([0, 3.2, 7.2])


def test_the_first_clip_has_no_incoming_transition():
    layout = layout_timeline([90, 90], [dissolve(0.8), TransitionRequest()], FPS, RULES)
    assert layout.placements[0].dissolve_frames == 0
    assert layout.adjusted() == []


def test_adjusted_lists_what_differs_from_the_request():
    layout = layout_timeline([90, 30, 90], [TransitionRequest(), dissolve(0.8), dissolve(0.8)], FPS, RULES)
    assert [p.index for p in layout.adjusted()] == [1, 2]  # both limited by the 1 s clip
    assert all(p.dissolve_frames == 7 for p in layout.adjusted())  # 25% of 30 frames


def test_forced_cut_turns_a_dissolve_into_a_cut():
    layout = layout_timeline([90, 90], [TransitionRequest(), dissolve(0.8)], FPS, RULES, forced_cuts=frozenset({1}))
    assert layout.placements[1].dissolve_frames == 0
    assert "block boundary" in layout.placements[1].note


def test_forced_cut_on_a_cut_adds_no_note():
    layout = layout_timeline([90, 90], [TransitionRequest(), TransitionRequest()], FPS, RULES, forced_cuts=frozenset({1}))
    assert layout.placements[1].note is None


def test_layout_is_exact_for_fractional_frame_rates():
    fps = Fraction(30000, 1001)
    layout = layout_timeline([60, 60], [TransitionRequest(), dissolve(0.8)], fps, RULES)
    assert layout.total_frames == 120 - layout.placements[1].dissolve_frames
    assert layout.duration == pytest.approx(layout.total_frames * 1001 / 30000)


def test_empty_and_mismatched_inputs():
    assert layout_timeline([], [], FPS, RULES).total_frames == 0
    with pytest.raises(ValueError):
        layout_timeline([30], [], FPS, RULES)
    with pytest.raises(ValueError):
        layout_timeline([0], [TransitionRequest()], FPS, RULES)


def layout_of(requests):
    return layout_timeline([90] * len(requests), requests, FPS, RULES)


def test_blocks_split_at_the_last_cut_that_fits():
    requests = [TransitionRequest(), dissolve(0.8), TransitionRequest(), dissolve(0.8), dissolve(0.8), TransitionRequest()]
    blocks, forced = split_blocks(layout_of(requests), block_size=3)
    assert blocks == [range(0, 2), range(2, 5), range(5, 6)]  # cuts into clip 2 and clip 5
    assert forced == frozenset()


def test_a_block_can_end_exactly_at_the_limit():
    requests = [TransitionRequest(), dissolve(0.8), dissolve(0.8), TransitionRequest(), TransitionRequest()]
    blocks, forced = split_blocks(layout_of(requests), block_size=3)
    assert blocks == [range(0, 3), range(3, 5)]
    assert forced == frozenset()


def test_a_window_of_dissolves_forces_a_cut_at_the_limit():
    blocks, forced = split_blocks(layout_of([TransitionRequest()] + [dissolve(0.8)] * 6), block_size=3)
    assert blocks == [range(0, 3), range(3, 6), range(6, 7)]
    assert forced == frozenset({3, 6})


def test_everything_in_one_block_when_it_fits():
    blocks, forced = split_blocks(layout_of([TransitionRequest()] + [dissolve(0.8)] * 3), block_size=20)
    assert blocks == [range(0, 4)]
    assert forced == frozenset()


def test_blocks_cover_every_clip_exactly_once():
    requests = [TransitionRequest()] + [dissolve(0.8) if i % 3 else TransitionRequest() for i in range(1, 40)]
    blocks, _ = split_blocks(layout_of(requests), block_size=7)
    covered = [index for block in blocks for index in block]
    assert covered == list(range(40))
    assert all(len(block) <= 7 for block in blocks)


def test_block_size_must_be_positive():
    with pytest.raises(ValueError):
        split_blocks(layout_of([TransitionRequest()]), 0)
