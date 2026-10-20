from fractions import Fraction

from render_fakes import RED, RUNNER, ffmpeg, solid_clip

from vacation_memories.render_checks import _timestamps_check, check_final, check_segment, failed
from vacation_memories.render_config import RenderConfig

CONFIG = RenderConfig()
FPS = Fraction(30)
SEGMENT_ARGS = ["-r", "30", "-video_track_timescale", "90000"]


def segment(tmp_path, seconds=2.0, size=(320, 180), name="seg.mp4", extra=()):
    return solid_clip(tmp_path / name, RED, seconds, size=size, fps=30,
                      codec_args=["-video_track_timescale", "90000", *extra])


def names(checks):
    return sorted(check.name for check in failed(checks))


def test_a_conforming_segment_passes(tmp_path):
    checks = check_segment(segment(tmp_path), RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60)
    assert failed(checks) == [] and len(checks) == 7


def test_each_deviation_of_a_segment_is_reported(tmp_path):
    path = segment(tmp_path)
    assert names(check_segment(path, RUNNER, CONFIG, size=(640, 360), fps=FPS, frames=60)) == ["resolution"]
    assert names(check_segment(path, RUNNER, CONFIG, size=(320, 180), fps=Fraction(25), frames=60)) == ["duration", "frame-rate"]
    assert names(check_segment(path, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=90)) == ["duration"]


def test_one_frame_of_difference_is_tolerated_two_are_not(tmp_path):
    path = segment(tmp_path)
    assert failed(check_segment(path, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=61)) == []
    assert names(check_segment(path, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=63)) == ["duration"]


def test_wrong_pixel_format_time_base_and_sar_are_reported(tmp_path):
    other_format = tmp_path / "p422.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv422p",
           "-video_track_timescale", "90000", str(other_format))
    assert names(check_segment(other_format, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60)) == ["pixel-format"]

    other_scale = solid_clip(tmp_path / "ts.mp4", RED, 2.0, fps=30, codec_args=["-video_track_timescale", "15360"])
    assert names(check_segment(other_scale, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60)) == ["time-base"]

    anamorphic = tmp_path / "sar.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=2", "-vf", "setsar=2", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", "-video_track_timescale", "90000", str(anamorphic))
    assert names(check_segment(anamorphic, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60)) == ["sar"]


def test_rotation_metadata_is_reported(tmp_path):
    from vacation_memories import synthetic_media as sm

    rotated = tmp_path / "rotated.mp4"
    sm.make_video(rotated, [(1, 2.0)], 320, 180, fps=30, rotation=90)
    assert "rotation" in names(check_segment(rotated, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60))


def test_variable_frame_rate_segment_is_reported(tmp_path):
    from render_fakes import vfr_clip

    assert "frame-rate" in names(check_segment(vfr_clip(tmp_path / "v.mp4"), RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60))


def test_unreadable_segment(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"x" * 500)
    assert names(check_segment(junk, RUNNER, CONFIG, size=(320, 180), fps=FPS, frames=60)) == ["readable"]


def final_file(tmp_path, *, seconds=2.0, audio=True, name="final.mp4", rate=48000, channels=2):
    inputs = ["-f", "lavfi", "-i", f"color=c=blue:s=320x180:r=30:d={seconds}"]
    if audio:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}"]
    args = [*inputs, "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-ac", str(channels)]
    ffmpeg(*args, str(tmp_path / name))
    return tmp_path / name


def final_checks(path, planned=2.0, **kwargs):
    return check_final(path, RUNNER, CONFIG, size=(320, 180), fps=FPS, planned_duration=planned, **kwargs)


def test_a_conforming_final_file_passes(tmp_path):
    checks = final_checks(final_file(tmp_path))
    assert failed(checks) == []
    assert {c.name for c in checks} == {"duration", "video-codec", "resolution", "frame-rate", "audio-format",
                                        "sync-start", "sync-duration", "timestamps"}


def test_duration_is_checked_against_the_plan_with_a_tolerance(tmp_path):
    path = final_file(tmp_path)
    assert failed(final_checks(path, planned=2.15)) == []
    assert names(final_checks(path, planned=2.5)) == ["duration"]


def test_missing_or_wrong_audio_is_reported(tmp_path):
    assert names(final_checks(final_file(tmp_path, audio=False))) == ["audio-stream"]
    assert failed(final_checks(final_file(tmp_path, audio=False, name="silent.mp4"), expect_audio=False)) == []
    assert names(final_checks(final_file(tmp_path, name="mono.mp4", channels=1))) == ["audio-format"]
    assert names(final_checks(final_file(tmp_path, name="44k.mp4", rate=44100))) == ["audio-format"]


def test_wrong_size_is_reported(tmp_path):
    checks = check_final(final_file(tmp_path), RUNNER, CONFIG, size=(640, 360), fps=FPS, planned_duration=2.0)
    assert names(checks) == ["resolution"]


def test_timestamp_check_on_packet_lists():
    ok = [(i / 30, i / 30) for i in range(10)]
    assert _timestamps_check(ok, FPS, CONFIG).ok
    reordered = [(2 / 30, -1 / 30), (0, 0), (1 / 30, 1 / 30), (4 / 30, 2 / 30), (3 / 30, 3 / 30)]  # B-frames: pts out of order, dts monotonic
    assert _timestamps_check(reordered, FPS, CONFIG).ok
    assert "no video packets" in _timestamps_check([], FPS, CONFIG).detail
    assert "not monotonic" in _timestamps_check([(0, 0), (1 / 30, 0), (2 / 30, 2 / 30)], FPS, CONFIG).detail
    assert "negative presentation" in _timestamps_check([(-0.5, -0.5), (0, 0)], FPS, CONFIG).detail
    assert "without a presentation" in _timestamps_check([(None, 0), (0.1, 0.1)], FPS, CONFIG).detail


def test_a_jump_above_two_frames_is_reported():
    jump = [(0, 0), (1 / 30, 1 / 30), (2 / 30, 2 / 30), (10 / 30, 10 / 30)]
    check = _timestamps_check(jump, FPS, CONFIG)
    assert not check.ok and "largest gap 0.267" in check.detail
    assert _timestamps_check([(0, 0), (2 / 30, 2 / 30)], FPS, CONFIG).ok  # exactly two frames is allowed
