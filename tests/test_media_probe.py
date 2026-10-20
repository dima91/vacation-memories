import pytest
from render_fakes import RED, RUNNER, ffmpeg, music_file, solid_clip, vfr_clip

from vacation_memories import synthetic_media as sm
from vacation_memories.media_probe import (
    ProbeError,
    is_variable_frame_rate,
    parse_video_stream,
    probe,
    stream_rotation,
    video_packet_times,
)


def test_probe_reads_a_clip(tmp_path):
    path = solid_clip(tmp_path / "a.mp4", RED, 1.0, size=(320, 180), fps=25)
    info = probe(path, RUNNER)
    video = info.video
    assert (video.width, video.height, video.pix_fmt, video.codec) == (320, 180, "yuv420p", "h264")
    assert video.r_frame_rate == 25 and video.avg_frame_rate == 25
    assert video.sample_aspect_ratio == 1 and video.rotation == 0 and not video.has_rotation_metadata
    assert video.displayed_size == (320, 180)
    assert not video.is_hdr and not video.is_high_bit_depth and not video.frame_rate_is_irregular
    assert info.audio is None
    assert info.format_duration == pytest.approx(1.0, abs=0.05)


def test_probe_reads_audio(tmp_path):
    info = probe(music_file(tmp_path / "m.wav", 1.5), RUNNER)
    assert info.video is None
    assert (info.audio.sample_rate, info.audio.channels) == (44100, 1)
    assert info.audio.duration == pytest.approx(1.5, abs=0.05)


def test_rotation_swaps_the_displayed_size(tmp_path):
    path = tmp_path / "rotated.mp4"
    sm.make_video(path, [(1, 1.0)], 320, 180, fps=10, rotation=90)
    video = probe(path, RUNNER).video
    assert (video.width, video.height) == (320, 180)  # stored
    assert video.rotation == 90 and video.has_rotation_metadata
    assert video.displayed_size == (180, 320)


def test_non_square_pixels_are_stretched_in_the_displayed_size(tmp_path):
    path = tmp_path / "anamorphic.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=240x180:r=10:d=1", "-vf", "setsar=4/3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    video = probe(path, RUNNER).video
    assert str(video.sample_aspect_ratio) == "4/3"
    assert video.displayed_size == (320, 180)


def test_high_bit_depth_and_hdr_flags(tmp_path):
    path = tmp_path / "hlg.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=64x64:r=10:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p10le",
           "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", "-color_primaries", "bt2020", str(path))
    video = probe(path, RUNNER).video
    assert video.is_high_bit_depth and video.is_hdr


def test_unreadable_file_raises_probe_error(tmp_path):
    path = tmp_path / "junk.mp4"
    path.write_bytes(b"junk" * 100)
    with pytest.raises(ProbeError):
        probe(path, RUNNER)
    with pytest.raises(ProbeError):
        probe(tmp_path / "missing.mp4", RUNNER)


@pytest.mark.parametrize(
    "stream, expected",
    [
        ({}, (0, False)),
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}, (270, True)),
        ({"side_data_list": [{"rotation": 90.0}]}, (90, True)),
        ({"side_data_list": [{"rotation": 180}]}, (180, True)),
        ({"tags": {"rotate": "90"}}, (270, True)),
        ({"tags": {"rotate": "x"}}, (0, False)),
        ({"side_data_list": [{"rotation": 90}], "tags": {"rotate": "180"}}, (90, True)),  # the matrix wins
        ({"side_data_list": [{"side_data_type": "Other"}]}, (0, False)),
    ],
)
def test_stream_rotation(stream, expected):
    assert stream_rotation(stream) == expected


def test_parse_video_stream_tolerates_missing_and_unknown_values():
    stream = parse_video_stream({"width": 10, "height": 20, "sample_aspect_ratio": "0:1", "r_frame_rate": "0/0",
                                 "avg_frame_rate": "N/A", "time_base": "1/90000"})
    assert stream.sample_aspect_ratio == 1 and stream.r_frame_rate == 0
    with pytest.raises(ProbeError):
        parse_video_stream({"width": 0, "height": 20})


def test_packet_times_are_listed_without_decoding(tmp_path):
    path = solid_clip(tmp_path / "a.mp4", RED, 1.0, fps=10)
    packets = video_packet_times(path, RUNNER, seconds=None)
    assert len(packets) == 10
    presentation = sorted(pts for pts, _ in packets)
    assert presentation == pytest.approx([i / 10 for i in range(10)])


def test_constant_rate_clip_is_not_variable(tmp_path):
    path = solid_clip(tmp_path / "cfr.mp4", RED, 2.0, fps=30)
    assert not is_variable_frame_rate(path, probe(path, RUNNER).video, RUNNER)


def test_irregular_timestamps_are_detected_as_variable(tmp_path):
    path = vfr_clip(tmp_path / "vfr.mp4")
    video = probe(path, RUNNER).video
    assert video.frame_rate_is_irregular
    assert is_variable_frame_rate(path, video, RUNNER)
