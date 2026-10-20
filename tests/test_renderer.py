import json
import threading
import time
from pathlib import Path

import pytest
from render_fakes import (
    BLUE,
    GREEN,
    MAGENTA,
    RED,
    YELLOW,
    close,
    color_at,
    ffmpeg,
    frame_at,
    halves_clip,
    halves_photo,
    make_plan,
    mean_luma,
    music_file,
    photo_element,
    probe_file,
    scenes_clip,
    solid_clip,
    vfr_clip,
    video_element,
    volume,
)

from vacation_memories import exit_codes
from vacation_memories import render_checks
from vacation_memories.edit_plan import PlanError
from vacation_memories.process import ToolRunner
from vacation_memories.render_config import RenderConfig
from vacation_memories.renderer import REPORT_NAME, RenderError, RenderOptions, render_plan

NO_FADES = RenderConfig(fade_in_seconds=0, fade_out_seconds=0)
PURE_RED, PURE_BLUE = (255, 0, 0), (0, 0, 255)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    solid_clip(root / "red.mp4", RED, 2.0)
    solid_clip(root / "blue.mp4", BLUE, 2.0)
    solid_clip(root / "red4.mp4", RED, 4.0)
    solid_clip(root / "blue4.mp4", BLUE, 4.0)
    solid_clip(root / "green4.mp4", GREEN, 4.0)
    solid_clip(root / "yellow12.mp4", YELLOW, 1.2)
    solid_clip(root / "blue12.mp4", BLUE, 1.2)
    solid_clip(root / "red07.mp4", RED, 0.7)
    solid_clip(root / "vertical.mp4", GREEN, 2.0, size=(180, 320))
    halves_clip(root / "halves.mp4", RED, BLUE, 2.0)
    halves_clip(root / "rotated.mp4", RED, BLUE, 2.0, rotation=90)
    scenes_clip(root / "scenes.mp4", [RED, GREEN, BLUE], 1.0)
    scenes_clip(root / "offset.mkv", [RED, GREEN, BLUE], 1.0, extra=["-output_ts_offset", "5"])
    vfr_clip(root / "vfr.mp4")
    halves_photo(root / "photo.jpg", PURE_RED, PURE_BLUE)
    halves_photo(root / "portrait.jpg", PURE_RED, PURE_BLUE, exif_orientation=6)
    halves_photo(root / "photo.heic", PURE_RED, PURE_BLUE)
    halves_photo(root / "framed.jpg", PURE_RED, PURE_BLUE, border=MAGENTA)
    music_file(root / "long.wav", 20.0)
    music_file(root / "short.wav", 2.0)
    return root


def render(plan, media, tmp_path, *, music=None, preview=False, name="out.mp4", config=NO_FADES, **options):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    opts = RenderOptions(config=config, music=music, preview=preview, **options)
    return render_plan(plan, input_dir=media, workdir=work, output=tmp_path / name, options=opts)


def frames_of(path) -> int:
    return round(probe_file(path).format_duration * 30)


# --- the basic cycle ----------------------------------------------------------------------------


def test_a_hand_written_plan_renders_to_a_verified_file(media, tmp_path):
    plan = make_plan([video_element("red.mp4", 0, 2), video_element("blue.mp4", 0, 2)])
    result = render(plan, media, tmp_path)

    assert result.ok and result.exit_code == exit_codes.OK
    assert result.output == tmp_path / "out.mp4" and result.output.exists()
    assert not (tmp_path / "out.partial.mp4").exists()
    info = probe_file(result.output)
    assert (info.video.width, info.video.height) == (320, 180)
    assert info.video.r_frame_rate == 30 and info.video.avg_frame_rate == 30
    assert info.format_duration == pytest.approx(4.0, abs=0.1)
    assert close(color_at(result.output, 0.5), PURE_RED) and close(color_at(result.output, 3.5), PURE_BLUE)

    report = json.loads((tmp_path / "work" / REPORT_NAME).read_text())
    assert report["ok"] and report["planned_duration"] == 4.0
    assert report["obtained_duration"] == pytest.approx(4.0, abs=0.1)
    assert {check["name"] for check in report["checks"]} >= {"duration", "sync-start", "timestamps", "audio-format"}
    assert all(check["ok"] for check in report["checks"])


def test_without_music_the_file_has_a_silent_audio_track_that_passes_the_checks(media, tmp_path):
    result = render(make_plan([video_element("red.mp4", 0, 2)]), media, tmp_path)
    audio = probe_file(result.output).audio
    assert (audio.codec, audio.sample_rate, audio.channels) == ("aac", 48000, 2)
    assert result.ok
    assert volume(result.output)[1] < -80  # digital silence


def test_original_files_are_untouched_and_nothing_is_written_next_to_them(media, tmp_path):
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in media.iterdir()}
    render(make_plan([video_element("red.mp4", 0, 2), photo_element("photo.jpg", 2)]), media, tmp_path)
    assert {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in media.iterdir()} == before


def test_temporary_files_are_removed_but_segments_are_kept(media, tmp_path):
    render(make_plan([video_element("red.mp4", 0, 2), photo_element("photo.jpg", 2)]), media, tmp_path)
    work = tmp_path / "work"
    assert list((work / "tmp").iterdir()) == []
    assert len(list((work / "segments").glob("*.mp4"))) == 2
    assert list((work / "segments").glob(".*")) == []


# --- final dimensions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size", [(1280, 720), (1080, 1920), (1080, 1080)], ids=["720p-16:9", "1080p-9:16", "1080p-1:1"]
)
def test_final_dimensions_with_horizontal_and_vertical_sources(media, tmp_path, size):
    plan = make_plan(
        [
            video_element("halves.mp4", 0, 1.5),
            video_element("vertical.mp4", 0, 1.5),
            photo_element("photo.jpg", 1.5),
            photo_element("portrait.jpg", 1.5),
        ],
        *size,
    )
    result = render(plan, media, tmp_path)
    assert result.ok
    video = probe_file(result.output).video
    assert (video.width, video.height) == size and video.sample_aspect_ratio == 1
    assert video.pix_fmt == "yuv420p" and video.rotation == 0 and not video.has_rotation_metadata
    assert probe_file(result.output).format_duration == pytest.approx(6.0, abs=0.15)


def test_other_proportions_go_on_a_blurred_background_not_on_black_bars(media, tmp_path):
    plan = make_plan([video_element("halves.mp4", 0, 1.0)], 540, 960)  # horizontal source, vertical output
    result = render(plan, media, tmp_path)
    frame = frame_at(result.output, 0.5)
    assert frame.size == (540, 960)
    assert mean_luma(frame, (0, 0.02, 1, 0.2)) > 40 and mean_luma(frame, (0, 0.8, 1, 0.98)) > 40  # bands are not black
    assert close(frame.getpixel((135, 480)), PURE_RED) and close(frame.getpixel((405, 480)), PURE_BLUE)  # the picture, whole
    assert close(frame.getpixel((100, 60)), PURE_RED, 90)  # the blurred copy follows the picture's colors
    assert close(frame.getpixel((440, 60)), PURE_BLUE, 90)


def test_matching_proportions_fill_the_frame(media, tmp_path):
    result = render(make_plan([video_element("halves.mp4", 0, 1.0)], 640, 360), media, tmp_path)
    frame = frame_at(result.output, 0.5)
    assert close(frame.getpixel((5, 5)), PURE_RED) and close(frame.getpixel((634, 354)), PURE_BLUE)


def test_small_sources_are_scaled_up_and_the_user_is_told(media, tmp_path, caplog):
    plan = make_plan([video_element("red.mp4", 0, 2)], 1280, 720)
    with caplog.at_level("WARNING"):
        result = render(plan, media, tmp_path)
    assert "have a resolution lower than the output and are scaled up" in caplog.text
    assert result.ok and probe_file(result.output).video.width == 1280


# --- transitions --------------------------------------------------------------------------------


def test_dissolve_middle_frame_is_a_mix_of_the_two_colors(media, tmp_path):
    plan = make_plan([video_element("red4.mp4", 0, 4), video_element("blue4.mp4", 0, 4, dissolve=1.0)])
    result = render(plan, media, tmp_path)
    assert result.ok
    assert close(color_at(result.output, 2.9), PURE_RED) and close(color_at(result.output, 4.1), PURE_BLUE)
    mix = color_at(result.output, 3.5)  # the dissolve covers 3.0 s - 4.0 s
    assert close(mix, (128, 0, 128), 45), mix
    assert mix[0] > 60 and mix[2] > 60  # neither color has taken over
    assert probe_file(result.output).format_duration == pytest.approx(7.0, abs=0.1)


def test_a_cut_switches_color_between_two_consecutive_frames(media, tmp_path):
    result = render(make_plan([video_element("red.mp4", 0, 2), video_element("blue.mp4", 0, 2)]), media, tmp_path)
    assert close(color_at(result.output, 1.95), PURE_RED) and close(color_at(result.output, 2.05), PURE_BLUE)


def test_very_short_clip_gets_the_dissolve_reduced_to_a_quarter(media, tmp_path):
    plan = make_plan([video_element("yellow12.mp4", 0, 1.2), video_element("blue12.mp4", 0, 1.2, dissolve=0.8)])
    result = render(plan, media, tmp_path)
    assert result.ok
    assert probe_file(result.output).format_duration == pytest.approx(1.2 + 1.2 - 0.3, abs=0.07)  # 25% of 1.2 s
    [adjusted] = result.report["transitions"]["adjusted"]
    assert adjusted["requested"] == {"type": "dissolve", "duration": 0.8}
    assert adjusted["effective"] == {"type": "dissolve", "duration": pytest.approx(0.3)}
    assert "reduced from 0.8 s to 0.30 s" in adjusted["reason"]


def test_dissolve_below_the_threshold_becomes_a_cut(media, tmp_path, caplog):
    plan = make_plan([video_element("red07.mp4", 0, 0.7), video_element("blue.mp4", 0, 2, dissolve=0.8)])
    with caplog.at_level("WARNING"):
        result = render(plan, media, tmp_path)
    assert result.ok and result.report["transitions"] == {
        "dissolves": 0, "cuts": 1,
        "adjusted": [{
            "source": "blue.mp4", "requested": {"type": "dissolve", "duration": 0.8},
            "effective": {"type": "cut", "duration": 0.0},
            "reason": result.report["transitions"]["adjusted"][0]["reason"],
        }],
    }
    assert "turned into a cut" in caplog.text
    assert probe_file(result.output).format_duration == pytest.approx(0.7 + 2.0, abs=0.07)  # nothing overlapped


def test_hand_written_transition_beyond_the_limits_is_reduced_with_a_warning(media, tmp_path, caplog):
    plan = make_plan([video_element("red4.mp4", 0, 4), video_element("blue4.mp4", 0, 4, dissolve=3.0)])
    with caplog.at_level("WARNING"):
        result = render(plan, media, tmp_path)
    assert "blue4.mp4: dissolve reduced from 3 s to 1.00 s" in caplog.text
    assert result.report["transitions"]["adjusted"][0]["effective"]["duration"] == pytest.approx(1.0)
    assert probe_file(result.output).format_duration == pytest.approx(7.0, abs=0.07)
    assert any("dissolve reduced" in w for w in result.report["warnings"])


def test_timeline_duration_is_the_sum_of_the_clips_minus_the_dissolves(media, tmp_path):
    plan = make_plan([
        video_element("red4.mp4", 0, 4),
        video_element("blue4.mp4", 0, 4, dissolve=0.8),
        video_element("green4.mp4", 0, 4, dissolve=0.5),
        photo_element("photo.jpg", 3.0),
    ])
    result = render(plan, media, tmp_path)
    expected = 4 + 4 + 4 + 3 - 0.8 - 0.5
    assert result.report["planned_duration"] == pytest.approx(expected, abs=0.04)
    assert probe_file(result.output).format_duration == pytest.approx(expected, abs=0.07)
    assert result.report["transitions"]["dissolves"] == 2 and result.report["transitions"]["cuts"] == 1


def test_destination_timestamps_match_the_video(media, tmp_path):
    from vacation_memories.edit_plan import finalize_timeline

    plan, layout = finalize_timeline(make_plan([
        video_element("red4.mp4", 0, 4), video_element("blue4.mp4", 0, 4, dissolve=1.0), video_element("green4.mp4", 0, 4),
    ]))
    result = render(plan, media, tmp_path)
    assert [e.timestamp for e in plan.elements] == pytest.approx([0.0, 3.0, 7.0])
    assert close(color_at(result.output, 2.9), PURE_RED) and close(color_at(result.output, 4.1), PURE_BLUE)
    assert close(color_at(result.output, 6.9), PURE_BLUE) and close(color_at(result.output, 7.1), (0, 255, 0))
    assert probe_file(result.output).format_duration == pytest.approx(plan.planned_duration, abs=0.07)


def test_video_fades_from_and_to_black(media, tmp_path):
    config = RenderConfig(fade_in_seconds=1.0, fade_out_seconds=1.5)
    result = render(make_plan([video_element("red4.mp4", 0, 4)]), media, tmp_path, config=config)
    assert result.ok
    assert sum(color_at(result.output, 0.0)) < 30  # black at the first frame
    assert close(color_at(result.output, 1.6), PURE_RED)
    assert sum(color_at(result.output, 3.95)) < 60  # nearly black at the end
    assert probe_file(result.output).format_duration == pytest.approx(4.0, abs=0.1)


# --- sources ------------------------------------------------------------------------------------


def test_frame_accurate_cut_with_a_nonzero_start_time(media, tmp_path):
    source = probe_file(media / "offset.mkv").video
    assert source.start_time == pytest.approx(5.0)  # the stream does not start at zero
    result = render(make_plan([video_element("offset.mkv", 1.0, 2.0)]), media, tmp_path)
    assert result.ok
    # times of the plan are relative to the first frame: the cut is exactly the green scene
    assert close(color_at(result.output, 0.0), (0, 255, 0)) and close(color_at(result.output, 0.95), (0, 255, 0))
    assert frames_of(result.output) == 30


def test_frame_accurate_cut_in_the_middle_of_a_group_of_pictures(media, tmp_path):
    assert probe_file(media / "scenes.mp4").video.start_time in (0.0, pytest.approx(0.0, abs=0.1))
    result = render(make_plan([video_element("scenes.mp4", 0.9, 2.1)]), media, tmp_path)  # starts in red, ends in blue
    assert close(color_at(result.output, 0.0), PURE_RED)  # frame 27 of the source is the last red one
    assert close(color_at(result.output, 0.09), PURE_RED) and close(color_at(result.output, 0.105), (0, 255, 0))
    assert close(color_at(result.output, 1.09), (0, 255, 0)) and close(color_at(result.output, 1.11), PURE_BLUE)
    assert frames_of(result.output) == 36


def test_variable_frame_rate_clip_is_converted_to_a_constant_one(media, tmp_path):
    source = probe_file(media / "vfr.mp4").video
    assert source.r_frame_rate != source.avg_frame_rate  # really variable
    result = render(make_plan([video_element("vfr.mp4", 0, 2.0)]), media, tmp_path)
    assert result.ok
    segment = next((tmp_path / "work" / "segments").glob("*.mp4"))
    video = probe_file(segment).video
    assert video.r_frame_rate == 30 and video.avg_frame_rate == 30
    assert probe_file(result.output).video.r_frame_rate == 30
    assert result.report["segments"]["vfr_converted"] == ["vfr.mp4"]


def test_rotation_metadata_is_applied_to_the_pixels_and_removed(media, tmp_path):
    stored = probe_file(media / "rotated.mp4").video
    assert (stored.width, stored.height, stored.rotation) == (320, 180, 90)
    result = render(make_plan([video_element("rotated.mp4", 0, 1.5)], 180, 320), media, tmp_path)
    assert result.ok
    segment = next((tmp_path / "work" / "segments").glob("*.mp4"))
    video = probe_file(segment).video
    assert (video.width, video.height) == (180, 320) and not video.has_rotation_metadata
    # What FFmpeg itself shows for the rotated clip is what the segment contains.
    reference = frame_at(media / "rotated.mp4", 0.5)
    assert reference.size == (180, 320) and reference.getpixel((90, 30)) != reference.getpixel((90, 290))
    frame = frame_at(result.output, 0.5)
    assert close(frame.getpixel((90, 30)), reference.getpixel((90, 30)), 40)
    assert close(frame.getpixel((90, 290)), reference.getpixel((90, 290)), 40)


def test_vertical_source_in_a_horizontal_edit_is_centered_without_distortion(media, tmp_path):
    plan = make_plan([video_element("rotated.mp4", 0, 1.0)], 640, 360)
    result = render(plan, media, tmp_path)
    frame = frame_at(result.output, 0.5)
    # the displayed picture is 180x320: a 202x360 strip in the middle of the frame
    reference = frame_at(media / "rotated.mp4", 0.5)
    assert close(frame.getpixel((320, 20)), reference.getpixel((90, 10)), 45)
    assert close(frame.getpixel((320, 340)), reference.getpixel((90, 310)), 45)


def picture_width(image) -> int:
    """Width of the sharp picture on the frame, from the pure magenta pixels of its border."""
    row = [image.getpixel((x, image.height // 2)) for x in range(image.width)]
    border = [x for x, pixel in enumerate(row) if close(pixel, MAGENTA, 45)]
    return max(border) + 1 - min(border)


def test_photos_get_a_ken_burns_zoom(media, tmp_path):
    result = render(make_plan([photo_element("framed.jpg", 3.0)], 640, 360), media, tmp_path)
    assert result.ok and frames_of(result.output) == 90
    # a 4:3 photo on a 16:9 frame is 480 px wide; the slow zoom grows it by 6% over the clip
    first, last = picture_width(frame_at(result.output, 0.0)), picture_width(frame_at(result.output, 2.95))
    assert first == pytest.approx(480, abs=4)
    assert last > first * 1.04 and last < first * 1.08


def test_consecutive_photos_alternate_zoom_direction(media, tmp_path):
    plan = make_plan([photo_element("framed.jpg", 2.0), photo_element("framed.jpg", 2.0)], 640, 360)
    result = render(plan, media, tmp_path)
    widths = [picture_width(frame_at(result.output, t)) for t in (0.0, 1.95, 2.0, 3.95)]
    assert widths[1] > widths[0] + 15  # the first photo zooms in
    assert widths[2] > widths[3] + 15  # the second zooms out, starting where the first ended


def test_photo_exif_orientation_is_respected(media, tmp_path):
    from PIL import Image, ImageOps

    expected = ImageOps.exif_transpose(Image.open(media / "portrait.jpg"))
    assert expected.size == (240, 320)
    top_color, bottom_color = expected.getpixel((120, 80)), expected.getpixel((120, 240))
    assert top_color != bottom_color

    result = render(make_plan([photo_element("portrait.jpg", 1.0)], 180, 320), media, tmp_path)
    frame = frame_at(result.output, 0.5)
    assert close(frame.getpixel((90, 100)), top_color, 40)
    assert close(frame.getpixel((90, 220)), bottom_color, 40)


def test_heic_photo(media, tmp_path):
    result = render(make_plan([photo_element("photo.heic", 1.0)], 320, 180), media, tmp_path)
    assert result.ok
    frame = frame_at(result.output, 0.5)
    assert close(frame.getpixel((100, 90)), PURE_RED, 40) and close(frame.getpixel((220, 90)), PURE_BLUE, 40)


def test_photo_colors_match_the_tags_of_the_output(media, tmp_path):
    result = render(make_plan([photo_element("photo.jpg", 1.0)], 320, 180), media, tmp_path)
    video = probe_file(result.output).video
    assert (video.color_space, video.color_transfer, video.color_primaries) == ("bt709", "bt709", "bt709")


def test_hdr_clip_is_tone_mapped_to_sdr(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p10le",
           "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", "-color_primaries", "bt2020", str(media / "hlg.mp4"))
    assert probe_file(media / "hlg.mp4").video.is_hdr
    result = render(make_plan([video_element("hlg.mp4", 0, 2)]), media, tmp_path)
    assert result.ok
    video = probe_file(result.output).video
    assert video.pix_fmt == "yuv420p" and not video.is_hdr and video.color_primaries == "bt709"
    assert result.report["segments"]["hdr_converted"] == ["hlg.mp4"]


def test_hdr_without_tone_mapping_filters_is_converted_with_a_warning(tmp_path, caplog):
    from vacation_memories.runtime_profile import DetectionEnv, detect_ffmpeg

    media = tmp_path / "media"
    media.mkdir()
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=320x180:r=30:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p10le",
           "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_primaries", "bt2020", str(media / "pq.mp4"))
    info = detect_ffmpeg(DetectionEnv(), lambda m: None)
    limited = type(info)(info.ffmpeg_version, info.ffprobe_version, info.encoders,
                         info.filters - {"zscale", "tonemap"}, info.hwaccels, info.hw_encoders)
    work = tmp_path / "work"
    work.mkdir()
    with caplog.at_level("WARNING"):
        result = render_plan(make_plan([video_element("pq.mp4", 0, 1)]), input_dir=media, workdir=work,
                             output=tmp_path / "o.mp4", options=RenderOptions(config=NO_FADES), ffmpeg=limited)
    assert result.ok and "without tone mapping" in caplog.text
    assert result.report["segments"]["hdr_converted"] == []


# --- music --------------------------------------------------------------------------------------


def level(path, start, length):
    return volume(path, start, length)[0]


def test_longer_music_is_cut_with_fades_and_ends_with_the_video(media, tmp_path):
    config = RenderConfig(fade_in_seconds=0.5, fade_out_seconds=1.5)
    result = render(make_plan([video_element("red4.mp4", 0, 4)]), media, tmp_path, music=media / "long.wav", config=config)
    assert result.ok and result.report["music"]["mode"] == "trimmed"
    info = probe_file(result.output)
    assert info.audio.duration == pytest.approx(4.0, abs=0.1) and info.video.duration == pytest.approx(4.0, abs=0.1)
    middle, start, end = level(result.output, 1.5, 1.0), level(result.output, 0.0, 0.2), level(result.output, 3.8, 0.2)
    assert middle > -25 and start < middle - 6 and end < middle - 12  # faded in and out


def test_shorter_music_loops_with_crossfades_and_a_warning(media, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        result = render(make_plan([video_element("red4.mp4", 0, 4)]), media, tmp_path, music=media / "short.wav")
    assert "shorter than the video" in caplog.text and "loops 3 times" in caplog.text
    assert result.ok and result.report["music"]["mode"] == "looped" and result.report["music"]["repeats"] == 3
    assert probe_file(result.output).audio.duration == pytest.approx(4.0, abs=0.1)
    assert level(result.output, 1.0, 2.0) > -25  # sound the whole way through


def test_unusable_music_is_a_usage_error(media, tmp_path):
    with pytest.raises(RenderError, match="no audio stream") as error:
        render(make_plan([video_element("red.mp4", 0, 2)]), media, tmp_path, music=media / "red.mp4")
    assert error.value.exit_code == exit_codes.USAGE
    with pytest.raises(RenderError, match="cannot read the music file"):
        render(make_plan([video_element("red.mp4", 0, 2)]), media, tmp_path, music=media / "missing.mp3")
    assert list((tmp_path / "work" / "segments").glob("*")) == []  # it failed before any work was done


# --- robustness ---------------------------------------------------------------------------------


def test_a_defective_segment_is_regenerated_then_skipped_and_the_render_goes_on(media, tmp_path, caplog):
    # The plan asks for 6 s of a 2 s clip: the segment is short, so it never conforms.
    plan = make_plan([
        video_element("red4.mp4", 0, 4),
        video_element("red.mp4", 0, 6, dissolve=0.8),
        video_element("blue4.mp4", 0, 4, dissolve=0.8),
    ])
    with caplog.at_level("WARNING"):
        result = render(plan, media, tmp_path)
    assert result.ok
    assert "regenerating with conservative parameters" in caplog.text and "skipping red.mp4" in caplog.text
    assert result.report["segments"]["skipped"][0]["source"] == "red.mp4"
    # The adjacent transition is recalculated between the clips that are left.
    assert probe_file(result.output).format_duration == pytest.approx(4 + 4 - 0.8, abs=0.07)
    assert close(color_at(result.output, 2.0), PURE_RED) and close(color_at(result.output, 5.0), PURE_BLUE)


def test_unreadable_or_missing_sources_are_skipped(media, tmp_path):
    bad = media / "corrupt.mp4"
    bad.write_bytes(b"\x00" * 3000)
    try:
        plan = make_plan([video_element("red.mp4", 0, 2), video_element("corrupt.mp4", 0, 2),
                          video_element("gone.mp4", 0, 2), photo_element("missing.jpg", 2), video_element("blue.mp4", 0, 2)])
        result = render(plan, media, tmp_path)
    finally:
        bad.unlink()
    assert result.ok
    assert [s["source"] for s in result.report["segments"]["skipped"]] == ["corrupt.mp4", "gone.mp4", "missing.jpg"]
    assert probe_file(result.output).format_duration == pytest.approx(4.0, abs=0.1)


def test_a_corrupt_photo_is_skipped(media, tmp_path):
    bad = media / "broken.jpg"
    bad.write_bytes(b"\xff\xd8junk" * 20)
    try:
        result = render(make_plan([photo_element("broken.jpg", 2), video_element("red.mp4", 0, 2)]), media, tmp_path)
    finally:
        bad.unlink()
    assert result.ok and result.report["segments"]["skipped"][0]["source"] == "broken.jpg"


def test_nothing_to_render_is_an_error(media, tmp_path):
    with pytest.raises(RenderError, match="nothing to render"):
        render(make_plan([video_element("gone.mp4", 0, 2)]), media, tmp_path)
    with pytest.raises(PlanError, match="no elements"):
        render(make_plan([]), media, tmp_path)


def test_failed_check_leaves_a_partial_file_and_exit_code_3(media, tmp_path, monkeypatch, caplog):
    bad = render_checks.Check("duration", False, "99 s, planned 4 s")
    monkeypatch.setattr("vacation_memories.renderer.check_final", lambda *a, **k: [bad])
    with caplog.at_level("WARNING"):
        result = render(make_plan([video_element("red.mp4", 0, 2)]), media, tmp_path)
    assert not result.ok and result.exit_code == exit_codes.VERIFICATION_FAILED
    assert result.output == tmp_path / "out.partial.mp4" and result.output.exists()
    assert not (tmp_path / "out.mp4").exists()  # never presented as successful
    assert "check failed: duration" in caplog.text
    report = json.loads((tmp_path / "work" / REPORT_NAME).read_text())
    assert report["ok"] is False and report["checks"] == [bad.to_dict()]


def test_a_failed_render_does_not_replace_an_existing_output(media, tmp_path, monkeypatch):
    existing = tmp_path / "out.mp4"
    existing.write_bytes(b"previous result")
    monkeypatch.setattr("vacation_memories.renderer.check_final",
                        lambda *a, **k: [render_checks.Check("duration", False, "x")])
    render(make_plan([video_element("red.mp4", 0, 2)]), media, tmp_path)
    assert existing.read_bytes() == b"previous result"


def test_output_location_rules(media, tmp_path):
    plan = make_plan([video_element("red.mp4", 0, 2)])
    work = tmp_path / "work"
    work.mkdir()

    result = render_plan(plan, input_dir=media, workdir=work, output=Path("rel.mp4"),
                         options=RenderOptions(config=NO_FADES))
    assert result.output == work / "rel.mp4"  # a relative output is resolved inside the workdir

    with pytest.raises(PlanError, match="inside the input folder"):
        render_plan(plan, input_dir=media, workdir=work, output=media / "out.mp4", options=RenderOptions(config=NO_FADES))
    with pytest.raises(PlanError, match="must be an .mp4"):
        render_plan(plan, input_dir=media, workdir=work, output=tmp_path / "out.mkv", options=RenderOptions(config=NO_FADES))
    with pytest.raises(PlanError, match="does not exist or is not writable"):
        render_plan(plan, input_dir=media, workdir=work, output=tmp_path / "nope" / "out.mp4", options=RenderOptions(config=NO_FADES))


def test_path_with_spaces_and_special_characters(tmp_path):
    media = tmp_path / "my media"
    media.mkdir()
    name = "day 1/clip (final) & more $HOME 'x'.mp4"
    (media / "day 1").mkdir()
    solid_clip(media / name, RED, 1.0)
    out = tmp_path / "out dir"
    out.mkdir()
    work = tmp_path / "work dir"
    work.mkdir()
    result = render_plan(make_plan([video_element(name, 0, 1)]), input_dir=media, workdir=work,
                         output=out / "my memories; ok.mp4", options=RenderOptions(config=NO_FADES))
    assert result.ok and (out / "my memories; ok.mp4").exists()


def test_non_utf8_file_name(tmp_path):
    import os

    media = tmp_path / "media"
    media.mkdir()
    raw = b"caf\xe9.mp4"
    solid_clip(media / "tmp.mp4", RED, 1.0)
    os.rename(media / "tmp.mp4", os.path.join(os.fsencode(media), raw))
    work = tmp_path / "work"
    work.mkdir()
    result = render_plan(make_plan([video_element(os.fsdecode(raw), 0, 1)]), input_dir=media, workdir=work,
                         output=tmp_path / "o.mp4", options=RenderOptions(config=NO_FADES))
    assert result.ok
    assert json.loads((work / REPORT_NAME).read_text())["segments"]["total"] == 1


# --- segments cache -----------------------------------------------------------------------------


class CountingRunner:
    """Counts the ffmpeg runs and the highest number running at once (each held for `delay` s)."""

    def __init__(self, monkeypatch, delay=0.0):
        self.calls, self.peak, self._active, self._lock = 0, 0, 0, threading.Lock()
        original = ToolRunner.ffmpeg

        def ffmpeg(runner, args, **kwargs):
            with self._lock:
                self.calls += 1
                self._active += 1
                self.peak = max(self.peak, self._active)
            try:
                time.sleep(delay)
                return original(runner, args, **kwargs)
            finally:
                with self._lock:
                    self._active -= 1

        monkeypatch.setattr(ToolRunner, "ffmpeg", ffmpeg)


def test_segments_are_reused_by_a_later_render(media, tmp_path, monkeypatch):
    plan = make_plan([video_element("red.mp4", 0, 2), photo_element("photo.jpg", 2), video_element("vfr.mp4", 0, 2)])
    first = render(plan, media, tmp_path)
    counter = CountingRunner(monkeypatch)
    second = render(plan, media, tmp_path, name="again.mp4")
    assert counter.calls == 1  # only the final encode
    assert second.report["segments"]["reused"] == 3 and first.report["segments"]["reused"] == 0
    assert second.report["segments"]["vfr_converted"] == ["vfr.mp4"]  # remembered with the segment
    assert probe_file(second.output).format_duration == pytest.approx(probe_file(first.output).format_duration, abs=0.04)


def test_changing_the_output_or_the_source_regenerates_the_segment(media, tmp_path, monkeypatch):
    work_media = tmp_path / "media"
    work_media.mkdir()
    solid_clip(work_media / "a.mp4", RED, 2.0)
    plan = make_plan([video_element("a.mp4", 0, 2)])
    render(plan, work_media, tmp_path)
    counter = CountingRunner(monkeypatch)
    assert render(plan, work_media, tmp_path, name="same.mp4").report["segments"]["reused"] == 1
    assert counter.calls == 1

    counter.calls = 0
    render(make_plan([video_element("a.mp4", 0, 2)], 640, 360), work_media, tmp_path, name="bigger.mp4")
    assert counter.calls == 2  # new resolution: a new segment and a new final file

    counter.calls = 0
    solid_clip(work_media / "a.mp4", BLUE, 2.0)  # the source changes
    result = render(plan, work_media, tmp_path, name="changed.mp4")
    assert result.report["segments"]["reused"] == 0 and close(color_at(result.output, 1.0), PURE_BLUE)


def test_a_damaged_cached_segment_is_made_again(media, tmp_path):
    plan = make_plan([video_element("red.mp4", 0, 2)])
    render(plan, media, tmp_path)
    segment = next((tmp_path / "work" / "segments").glob("*.mp4"))
    segment.write_bytes(segment.read_bytes()[:500])
    result = render(plan, media, tmp_path, name="again.mp4")
    assert result.ok and result.report["segments"]["reused"] == 0


# --- jobs, blocks, preview, encoders ------------------------------------------------------------


def test_jobs_run_segments_in_parallel(media, tmp_path, monkeypatch):
    plan = make_plan([video_element(f, 0, 2) for f in ("red.mp4", "blue.mp4", "halves.mp4", "vertical.mp4")])
    counter = CountingRunner(monkeypatch, delay=0.3)
    result = render(plan, media, tmp_path, jobs=3, threads_per_job=1)
    assert result.ok and 2 <= counter.peak <= 3

    sequential = CountingRunner(monkeypatch, delay=0.05)
    other = tmp_path / "sequential"
    other.mkdir()
    render(plan, media, other, jobs=1)
    assert sequential.peak == 1


def test_threads_per_job_reach_decoder_filters_and_encoder(media, tmp_path, monkeypatch):
    seen = []
    original = ToolRunner.run
    monkeypatch.setattr(ToolRunner, "run", lambda self, command, **kw: seen.append(command) or original(self, command, **kw))
    render(make_plan([video_element("red.mp4", 0, 2)]), media, tmp_path, threads_per_job=2)
    segment, final = [c for c in seen if c[0] == "ffmpeg" and "-filter_complex" in c]
    for command in (segment, final):
        assert command.count("-threads") == 2  # decoder (before the input) and encoder (before the output)
        assert command[command.index("-filter_complex_threads") + 1] == "2"
        assert command.index("-threads") < command.index("-i") < command.index("-filter_complex") < len(command) - 3
        assert command[-3:-1] == ["-threads", "2"]


def test_long_timelines_are_assembled_in_blocks_split_at_cuts(media, tmp_path):
    names = ["red.mp4", "blue.mp4", "halves.mp4", "red.mp4", "blue.mp4", "halves.mp4", "red.mp4"]
    plan = make_plan([video_element(n, 0, 2, dissolve=0.0 if i in (3, 5) else 0.5) for i, n in enumerate(names)])
    result = render(plan, media, tmp_path, block_size=3)
    assert result.ok and result.report["blocks"] == [3, 2, 2]  # blocks end at the cuts into clips 3 and 5
    assert result.report["block_boundaries_forced"] == []
    # clips 0-2 joined by 2 dissolves, 3-4 by 1, 5-6 by 1; 7*2 s minus 4 dissolves of 0.5 s
    assert probe_file(result.output).format_duration == pytest.approx(14 - 2.0, abs=0.1)
    assert list((tmp_path / "work" / "tmp").iterdir()) == []


def test_a_block_boundary_inside_a_run_of_dissolves_becomes_a_cut(media, tmp_path, caplog):
    plan = make_plan([video_element("red4.mp4", 0, 4)] + [video_element("blue4.mp4", 0, 4, dissolve=1.0)] * 4)
    with caplog.at_level("WARNING"):
        result = render(plan, media, tmp_path, block_size=3)
    assert result.report["blocks"] == [3, 2] and result.report["block_boundaries_forced"] == [3]
    assert "dissolve turned into a cut at a block boundary" in caplog.text
    assert result.ok and probe_file(result.output).format_duration == pytest.approx(20 - 3.0, abs=0.1)


def test_blocks_and_a_single_pass_give_the_same_picture(media, tmp_path):
    plan = make_plan([video_element("red.mp4", 0, 2), video_element("blue.mp4", 0, 2), video_element("halves.mp4", 0, 2)])
    whole = render(plan, media, tmp_path, name="whole.mp4")
    other = tmp_path / "split"
    other.mkdir()
    split = render(plan, media, other, name="split.mp4", block_size=1)
    assert split.report["blocks"] == [1, 1, 1]
    assert frames_of(whole.output) == frames_of(split.output)
    for t in (0.5, 2.5, 4.5):
        assert close(color_at(whole.output, t), color_at(split.output, t), 8)


def test_preview_is_small_fast_and_does_not_overwrite_the_final_file(media, tmp_path):
    plan = make_plan([video_element("red4.mp4", 0, 4), photo_element("photo.jpg", 2)], 1920, 1080, 30)
    final = render(plan, media, tmp_path, name="memories.mp4")
    preview = render(plan, media, tmp_path, name="memories.mp4", preview=True)
    assert final.output == tmp_path / "memories.mp4" and preview.output == tmp_path / "memories_preview.mp4"
    assert final.output.exists() and preview.output.exists()
    video = probe_file(preview.output).video
    assert (video.width, video.height) == (960, 540) and video.r_frame_rate == 24
    assert probe_file(preview.output).format_duration == pytest.approx(6.0, abs=0.15)
    assert preview.report["preview"] is True and preview.report["output_spec"]["fps"] == 24
    assert probe_file(final.output).video.width == 1920  # untouched


def test_preview_of_a_portrait_plan_keeps_the_proportions(media, tmp_path):
    result = render(make_plan([video_element("red.mp4", 0, 2)], 1080, 1920), media, tmp_path, preview=True)
    video = probe_file(result.output).video
    assert (video.width, video.height) == (540, 960)


def test_unavailable_hardware_encoding_falls_back_to_software_and_says_so(media, tmp_path, caplog):
    from vacation_memories.runtime_profile import DetectionEnv, detect_ffmpeg

    info = detect_ffmpeg(DetectionEnv(), lambda m: None)
    work = tmp_path / "work"
    work.mkdir()
    with caplog.at_level("WARNING"):
        result = render_plan(make_plan([video_element("red.mp4", 0, 2)]), input_dir=media, workdir=work, output=tmp_path / "o.mp4",
                             options=RenderOptions(config=NO_FADES, hwaccel="auto"), ffmpeg=info)
    assert result.ok and probe_file(result.output).video.codec == "h264"
    assert result.report["encoder"]["name"] == "libx264" and result.report["encoder"]["requested_hwaccel"] == "auto"
    assert "hardware encoding does not work here" in result.report["encoder"]["fallback_reason"]
    assert "using libx264" in caplog.text


def test_missing_ffmpeg_features_are_a_requirements_error(media, tmp_path):
    from vacation_memories.runtime_profile import DetectionEnv, detect_ffmpeg

    info = detect_ffmpeg(DetectionEnv(), lambda m: None)
    no_x264 = type(info)(info.ffmpeg_version, info.ffprobe_version, info.encoders - {"libx264"}, info.filters, (), ())
    with pytest.raises(RenderError, match="libx264") as error:
        render_plan(make_plan([video_element("red.mp4", 0, 2)]), input_dir=media, workdir=tmp_path, output=tmp_path / "o.mp4", ffmpeg=no_x264)
    assert error.value.exit_code == exit_codes.REQUIREMENTS
    no_xfade = type(info)(info.ffmpeg_version, info.ffprobe_version, info.encoders, info.filters - {"xfade"}, (), ())
    with pytest.raises(RenderError, match="filter xfade"):
        render_plan(make_plan([video_element("red.mp4", 0, 2)]), input_dir=media, workdir=tmp_path, output=tmp_path / "o.mp4", ffmpeg=no_xfade)


def test_status_file_follows_the_phases(media, tmp_path):
    from vacation_memories.workdir import StatusWriter

    work = tmp_path / "work"
    work.mkdir()
    status = StatusWriter(work / "status.json")
    status.start()
    seen = []
    original = status.update
    status.update = lambda **fields: (seen.append(fields), original(**fields))[1]
    plan = make_plan([video_element("red.mp4", 0, 2), video_element("blue.mp4", 0, 2)])
    render_plan(plan, input_dir=media, workdir=work, output=tmp_path / "o.mp4", options=RenderOptions(config=NO_FADES), status=status)
    phases = []
    for fields in seen:
        if fields.get("phase") and (not phases or phases[-1] != fields["phase"]):
            phases.append(fields["phase"])
    assert phases == ["normalizing segments", "encoding the video", "verifying the file"]
    assert any(f.get("done") == 2 and f.get("total") == 2 for f in seen)
    assert status.read()["state"] == "done"
