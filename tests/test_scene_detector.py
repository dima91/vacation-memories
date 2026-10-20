import cv2
import numpy as np
import pytest

from vacation_memories import synthetic_media as sm
from vacation_memories.scene_detector import SceneCutDetector, TimeSpan, build_segments


def bgr_frame(scene, t=0.0):
    rgb = np.asarray(sm.render_scene_frame(scene, 160, 120, t=t))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def test_clip_without_cuts_is_one_segment():
    assert build_segments([], 0.0, 6.0) == [TimeSpan(0.0, 6.0)]


def test_cuts_split_the_clip_into_contiguous_scenes():
    segments = build_segments([2.0, 4.5], 0.0, 6.0)
    assert segments == [TimeSpan(0.0, 2.0), TimeSpan(2.0, 4.5), TimeSpan(4.5, 6.0)]


def test_cuts_outside_the_clip_and_duplicates_are_ignored():
    assert build_segments([0.0, 3.0, 3.0, 9.0, -1.0], 0.0, 6.0) == [TimeSpan(0.0, 3.0), TimeSpan(3.0, 6.0)]


def test_long_scenes_are_split_in_equal_windows_without_gaps():
    segments = build_segments([], 0.0, 25.0, max_segment_seconds=10.0)
    assert len(segments) == 3
    assert all(s.duration == pytest.approx(25.0 / 3) for s in segments)
    assert segments[0].start == 0.0 and segments[-1].end == 25.0
    assert all(a.end == b.start for a, b in zip(segments, segments[1:]))


def test_a_scene_of_exactly_the_maximum_length_is_not_split():
    assert build_segments([], 0.0, 10.0, max_segment_seconds=10.0) == [TimeSpan(0.0, 10.0)]


def test_clip_without_duration_is_rejected():
    with pytest.raises(ValueError):
        build_segments([], 1.0, 1.0)


def test_detector_finds_the_cut_between_two_scenes():
    detector = SceneCutDetector(fps=10)
    for index in range(40):
        scene = 1 if index < 20 else 2
        detector.feed(index, bgr_frame(scene, t=index / 10))
    assert detector.cut_frames == [20]


def test_detector_ignores_motion_inside_a_scene():
    detector = SceneCutDetector(fps=10)
    for index in range(40):
        detector.feed(index, bgr_frame(3, t=index / 10))
    assert detector.cut_frames == []


def test_detector_works_when_frames_are_fed_with_a_stride():
    detector = SceneCutDetector(fps=30)
    for index in range(0, 120, 3):
        detector.feed(index, bgr_frame(1 if index < 60 else 4, t=index / 30))
    assert detector.cut_frames == [60]


def test_cuts_closer_than_the_minimum_scene_length_are_merged():
    detector = SceneCutDetector(fps=10, min_scene_seconds=1.0)
    scenes = [1] * 20 + [2] * 3 + [5] * 20
    for index, scene in enumerate(scenes):
        detector.feed(index, bgr_frame(scene))
    assert len(detector.cut_frames) <= 1
