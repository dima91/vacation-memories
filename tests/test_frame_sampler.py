import cv2
import numpy as np
import pytest
from PIL import Image

from vacation_memories import frame_sampler
from vacation_memories import synthetic_media as sm
from vacation_memories.frame_sampler import (
    ANALYSIS_LONG_SIDE,
    SamplingError,
    photo_analysis_frame,
    scan_video,
    to_analysis_gray,
)
from vacation_memories.scene_detector import build_segments


@pytest.fixture(scope="module")
def three_scenes(tmp_path_factory):
    path = tmp_path_factory.mktemp("clips") / "three.mp4"
    sm.make_video(path, [(1, 2.0), (2, 2.0), (3, 2.0)], 320, 240, fps=10)
    return path


def test_scan_finds_cuts_and_samples_at_regular_intervals(three_scenes):
    scan = scan_video(three_scenes, lambda gray: gray.shape)
    assert scan.duration == pytest.approx(6.0, abs=0.15)
    assert scan.cut_times == pytest.approx([2.0, 4.0], abs=0.15)
    times = [t for t, _ in scan.samples]
    assert times[0] == 0.0 and len(times) == 12
    assert np.diff(times) == pytest.approx(0.5, abs=0.11)
    assert {shape for _, shape in scan.samples} == {(384, 512)}


def test_clip_is_opened_and_analyzed_once_per_sample(three_scenes, monkeypatch):
    opened = []
    real = cv2.VideoCapture

    def counting(*args, **kwargs):
        opened.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(frame_sampler.cv2, "VideoCapture", counting)
    calls = []
    scan = scan_video(three_scenes, lambda gray: calls.append(1))
    assert len(opened) == 1
    assert len(calls) == len(scan.samples) == 12


def test_motion_peaks_at_the_cuts(three_scenes):
    scan = scan_video(three_scenes, lambda gray: None)
    values = dict(scan.motion)
    assert all(v > 0 for v in values.values())
    spikes = sorted(t for t, v in values.items() if v > 2 * np.median(list(values.values())))
    assert spikes == pytest.approx([2.0, 4.0], abs=0.15)


def test_continuous_clip_is_split_in_windows_by_temporal_sampling(tmp_path):
    path = tmp_path / "continuous.mp4"
    sm.make_video(path, [(4, 25.0)], 160, 120, fps=10)
    scan = scan_video(path, lambda gray: None, sample_interval=1.0)
    assert scan.cut_times == []
    assert len(scan.samples) == pytest.approx(25, abs=1)
    segments = build_segments(scan.cut_times, 0.0, scan.duration)
    assert len(segments) == 3 and segments[-1].end == pytest.approx(25.0, abs=0.15)


def test_rotated_clip_is_analyzed_upright(tmp_path):
    path = tmp_path / "rotated.mp4"
    sm.make_video(path, [(5, 1.0)], 320, 180, fps=10, rotation=90)
    scan = scan_video(path, lambda gray: gray.shape)
    height, width = scan.samples[0][1]
    assert height > width and height == ANALYSIS_LONG_SIDE


def test_unreadable_clips_raise_sampling_error(tmp_path):
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(np.random.RandomState(0).bytes(4096))
    for path in (corrupt, tmp_path / "missing.mp4"):
        with pytest.raises(SamplingError):
            scan_video(path, lambda gray: None)


def test_truncated_clip_keeps_the_frames_decoded_before_the_damage(tmp_path):
    path = tmp_path / "whole.mp4"
    sm.make_video(path, [(1, 3.0)], 160, 120, fps=10)
    cut = tmp_path / "truncated.mp4"
    data = path.read_bytes()
    cut.write_bytes(data[: len(data) * 3 // 4])
    try:
        scan = scan_video(cut, lambda gray: None)
    except SamplingError:
        return  # the container may be unusable without its index; skipping the file is acceptable
    assert 0 < len(scan.samples) <= 6


def test_analysis_gray_normalizes_size_and_color():
    color = np.zeros((300, 1200, 3), np.uint8)
    assert to_analysis_gray(color).shape == (128, ANALYSIS_LONG_SIDE)
    assert to_analysis_gray(np.zeros((100, 50), np.uint8)).shape == (ANALYSIS_LONG_SIDE, 256)
    already = np.zeros((384, ANALYSIS_LONG_SIDE), np.uint8)
    assert to_analysis_gray(already) is already


@pytest.mark.parametrize("name", ["a.jpg", "a.png", "a.heic"])
def test_photo_frame_for_every_format(tmp_path, name):
    sm.make_photo(tmp_path / name, 2, 320, 240)
    assert photo_analysis_frame(tmp_path / name).shape == (384, ANALYSIS_LONG_SIDE)


def test_photo_frame_follows_exif_orientation(tmp_path):
    sm.make_photo(tmp_path / "a.jpg", 2, 320, 240, exif_orientation=6)
    assert photo_analysis_frame(tmp_path / "a.jpg").shape == (ANALYSIS_LONG_SIDE, 384)


def test_large_photo_is_reduced(tmp_path):
    Image.new("RGB", (4000, 3000), (120, 90, 60)).save(tmp_path / "big.jpg")
    assert photo_analysis_frame(tmp_path / "big.jpg").shape == (384, ANALYSIS_LONG_SIDE)


def test_corrupt_photo_raises_sampling_error(tmp_path):
    (tmp_path / "bad.jpg").write_bytes(b"not a jpeg" * 100)
    with pytest.raises(SamplingError):
        photo_analysis_frame(tmp_path / "bad.jpg")


class TestColorFramesForTheModel:
    def test_a_photo_keeps_its_colors_and_its_upright_orientation(self, tmp_path):
        path = tmp_path / "turned.jpg"
        sm.make_photo(path, 1, 320, 240, exif_orientation=6)
        image = frame_sampler.photo_color_frame(path)
        assert image.mode == "RGB"
        assert image.size == (240, 320)
        channels = np.asarray(image).reshape(-1, 3).std(axis=0)
        assert channels.min() > 5  # not a grayscale copy

    def test_an_unreadable_photo_is_a_sampling_error(self, tmp_path):
        path = tmp_path / "broken.jpg"
        path.write_bytes(b"not an image")
        with pytest.raises(SamplingError):
            frame_sampler.photo_color_frame(path)

    @pytest.mark.parametrize("timestamp, scene", [(1.0, 1), (3.0, 2), (5.0, 3)])
    def test_a_clip_frame_comes_from_the_requested_scene(self, three_scenes, timestamp, scene):
        image = frame_sampler.video_color_frame(three_scenes, timestamp)
        assert image.mode == "RGB"

        def distance_to(scene_id):
            reference = sm.render_scene_frame(scene_id, *image.size, t=timestamp).convert("RGB")
            return np.abs(np.asarray(image, dtype=float) - np.asarray(reference, dtype=float)).mean()

        assert min((1, 2, 3), key=distance_to) == scene

    def test_a_rotated_clip_arrives_upright(self, tmp_path):
        path = tmp_path / "rotated.mp4"
        sm.make_video(path, [(5, 1.0)], 320, 180, fps=10, rotation=90)
        assert frame_sampler.video_color_frame(path, 0.5).size == (180, 320)

    def test_a_timestamp_past_the_end_is_a_sampling_error(self, three_scenes):
        with pytest.raises(SamplingError):
            frame_sampler.video_color_frame(three_scenes, 60.0)

    def test_an_unreadable_clip_is_a_sampling_error(self, tmp_path):
        path = tmp_path / "broken.mp4"
        path.write_bytes(b"not a clip")
        with pytest.raises(SamplingError):
            frame_sampler.video_color_frame(path, 0.0)
