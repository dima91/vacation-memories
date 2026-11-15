"""Rendering what the editor writes into a plan: effects on any clip, and the music with tracks, parts and shift."""

import json

import pytest
from render_fakes import (
    BLUE,
    GREEN,
    RED,
    close,
    color_at,
    make_plan,
    music_file,
    probe_file,
    solid_clip,
    video_element,
    volume,
)

from vacation_memories.core.config import DEFAULT_FADE_IN_SECONDS
from vacation_memories.planning.edit_plan import (
    DIP,
    FADE_IN,
    FADE_OUT,
    EdgeFades,
    Effect,
    MusicPart,
    MusicPlan,
    MusicTrack,
    plan_from_dict,
)
from vacation_memories.planning.transitions import USER
from vacation_memories.rendering.render_config import RenderConfig
from vacation_memories.rendering.renderer import PREVIEW_REPORT_NAME, REPORT_NAME, RenderOptions, render_plan

NO_FADES = RenderConfig(fade_in_seconds=0, fade_out_seconds=0)
PURE_RED, PURE_BLUE, PURE_GREEN = (255, 0, 0), (0, 0, 255), (0, 255, 0)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    solid_clip(root / "red4.mp4", RED, 4.0)
    solid_clip(root / "blue4.mp4", BLUE, 4.0)
    solid_clip(root / "green4.mp4", GREEN, 4.0)
    return root


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("music")
    music_file(root / "loud.wav", 10.0, 440, 1.0)
    music_file(root / "quiet.wav", 10.0, 880, 0.1)
    music_file(root / "long.wav", 30.0)
    return root


def render(plan, media, tmp_path, *, config=NO_FADES, name="out.mp4", **options):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return render_plan(plan, input_dir=media, workdir=work, output=tmp_path / name,
                       options=RenderOptions(config=config, **options))


def fx(kind: str, offset: float, duration: float, identifier: str | None = None, origin: str = USER) -> Effect:
    return Effect(identifier or f"{kind}-{offset}", kind, offset, duration, origin)


def clip(source: str, *effects: Effect):
    return video_element(source, 0, 4, effects=effects)


def dark(path, seconds: float) -> bool:
    return sum(color_at(path, seconds)) < 60


# --- effects ---------------------------------------------------------------------------------------------


def test_a_fade_out_in_the_middle_of_the_timeline_goes_to_black_and_the_next_clip_cuts_in(media, tmp_path):
    plan = make_plan([clip("red4.mp4", fx(FADE_OUT, 3.0, 1.0)), clip("blue4.mp4")])
    result = render(plan, media, tmp_path)
    assert result.ok
    assert close(color_at(result.output, 2.8), PURE_RED) and dark(result.output, 3.97)
    assert close(color_at(result.output, 4.1), PURE_BLUE)  # the next clip is not touched
    assert probe_file(result.output).format_duration == pytest.approx(8.0, abs=0.1)


def test_a_fade_in_in_the_middle_of_the_timeline_comes_from_black(media, tmp_path):
    plan = make_plan([clip("red4.mp4"), clip("blue4.mp4", fx(FADE_IN, 0.0, 1.0))])
    result = render(plan, media, tmp_path)
    assert result.ok
    assert close(color_at(result.output, 3.9), PURE_RED) and dark(result.output, 4.0)
    assert close(color_at(result.output, 5.3), PURE_BLUE)


def test_a_dip_goes_to_black_and_back_in_the_middle_of_a_clip(media, tmp_path):
    result = render(make_plan([clip("red4.mp4", fx(DIP, 1.5, 1.0))]), media, tmp_path)
    assert result.ok
    assert close(color_at(result.output, 1.3), PURE_RED) and close(color_at(result.output, 2.8), PURE_RED)
    assert dark(result.output, 2.0)
    assert 30 < sum(color_at(result.output, 1.75)) < 220 and 30 < sum(color_at(result.output, 2.25)) < 220
    assert probe_file(result.output).format_duration == pytest.approx(4.0, abs=0.1)


def test_the_fades_of_the_first_and_last_clip_are_the_ones_of_the_plan(media, tmp_path):
    plan = make_plan([clip("red4.mp4", fx(FADE_IN, 0.0, 1.0), fx(FADE_OUT, 2.5, 1.5))])
    result = render(plan, media, tmp_path)
    assert result.ok and dark(result.output, 0.0)
    assert close(color_at(result.output, 1.6), PURE_RED) and dark(result.output, 3.95)


def test_without_effects_the_video_has_no_fades_whatever_the_configuration(media, tmp_path):
    config = RenderConfig(fade_in_seconds=1.0, fade_out_seconds=1.5)
    result = render(make_plan([clip("red4.mp4")]), media, tmp_path, config=config)
    assert close(color_at(result.output, 0.0), PURE_RED) and close(color_at(result.output, 3.95), PURE_RED)


def test_a_schema_1_plan_still_fades_from_and_to_black_as_before(media, tmp_path):
    data = {
        "schema_version": 1, "output": {"width": 320, "height": 180, "fps": 30},
        "elements": [{"source": "red4.mp4", "type": "video", "start": 0, "end": 4}],
    }
    plan = plan_from_dict(data, fades=EdgeFades(1.0, 1.5))
    result = render(plan, media, tmp_path)
    assert dark(result.output, 0.0) and close(color_at(result.output, 1.6), PURE_RED) and dark(result.output, 3.95)


def test_the_effects_are_applied_when_the_timeline_is_assembled_in_blocks(media, tmp_path):
    plan = make_plan([clip("red4.mp4", fx(FADE_OUT, 3.0, 1.0)), clip("blue4.mp4", fx(DIP, 1.5, 1.0)),
                      clip("green4.mp4", fx(FADE_IN, 0.0, 1.0))])
    result = render(plan, media, tmp_path, block_size=1)
    assert result.report["blocks"] == [1, 1, 1] and result.ok
    assert dark(result.output, 3.97) and dark(result.output, 6.0) and dark(result.output, 8.0)
    assert close(color_at(result.output, 4.4), PURE_BLUE) and close(color_at(result.output, 10.0), PURE_GREEN)


def test_a_dip_beside_a_dissolve_is_reduced_to_the_free_space_and_the_report_says_so(media, tmp_path, caplog):
    blue = video_element("blue4.mp4", 0, 4, dissolve=0.8)
    plan = make_plan([clip("red4.mp4", fx(DIP, 2.5, 1.5, "fx-dip")), blue])
    with caplog.at_level("WARNING"):
        result = render(plan, media, tmp_path)
    assert result.ok and "fx-dip" in caplog.text
    [adjusted] = result.report["effects"]["adjusted"]
    assert adjusted["id"] == "fx-dip" and adjusted["requested"] == {"offset": 2.5, "duration": 1.5}
    assert adjusted["effective"]["duration"] == pytest.approx(0.7, abs=0.04)
    assert close(color_at(result.output, 2.4), PURE_RED)


def test_a_fade_out_on_a_clip_that_ends_in_a_dissolve_is_left_out(media, tmp_path):
    blue = video_element("blue4.mp4", 0, 4, dissolve=0.8)
    result = render(make_plan([clip("red4.mp4", fx(FADE_OUT, 2.5, 1.5, "fx-out")), blue]), media, tmp_path)
    assert result.ok and result.report["effects"]["adjusted"][0]["effective"] is None
    assert close(color_at(result.output, 3.0), PURE_RED)


def test_an_effect_longer_than_its_clip_is_reduced_to_the_clip(media, tmp_path):
    result = render(make_plan([clip("red4.mp4", fx(FADE_IN, 0.0, 10.0, "fx-long"))]), media, tmp_path)
    assert result.ok
    [adjusted] = result.report["effects"]["adjusted"]
    assert adjusted["effective"] == {"offset": 0.0, "duration": 4.0}
    assert result.report["effects"]["applied"] == [{"id": "fx-long", "type": FADE_IN, "offset": 0.0, "duration": 4.0}]


def test_an_effect_below_the_minimum_is_left_out(media, tmp_path):
    result = render(make_plan([clip("red4.mp4", fx(FADE_IN, 0.0, 0.1))]), media, tmp_path)
    assert result.ok and close(color_at(result.output, 0.0), PURE_RED)
    assert result.report["effects"]["adjusted"][0]["effective"] is None


# --- the preview report ----------------------------------------------------------------------------------


def test_a_preview_writes_its_own_report_and_leaves_the_one_of_the_final_render(media, tmp_path):
    plan = make_plan([clip("red4.mp4")])
    final = render(plan, media, tmp_path, name="memories.mp4")
    work = tmp_path / "work"
    before = (work / REPORT_NAME).read_text()
    preview = render(plan, media, tmp_path, name="memories.mp4", preview=True)
    assert final.ok and preview.ok
    assert (work / REPORT_NAME).read_text() == before
    assert json.loads((work / PREVIEW_REPORT_NAME).read_text())["preview"] is True
    assert json.loads(before)["preview"] is False


# --- music of the plan ---------------------------------------------------------------------------------------


def music_plan(*tracks: MusicTrack, offset: float = 0.0, elements=None) -> object:
    return make_plan(elements or [clip("red4.mp4"), clip("blue4.mp4")], music=MusicPlan(tuple(tracks), offset))


def level(path, start, length):
    return volume(path, start, length)[0]


def play(plan, media, library, tmp_path, **options):
    result = render(plan, media, tmp_path, music_dir=library, **options)
    assert result.ok
    return result


def test_the_tracks_play_in_order_after_the_shift_and_silence_fills_the_rest(media, library, tmp_path):
    plan = music_plan(MusicTrack("loud.wav", (MusicPart(0.0, 3.0),)), MusicTrack("quiet.wav", (MusicPart(0.0, 3.0),)),
                      offset=1.0)
    result = play(plan, media, library, tmp_path)
    assert level(result.output, 0.1, 0.7) < -60  # the shift: silence first
    loud, quiet = level(result.output, 1.3, 2.4), level(result.output, 4.3, 2.4)
    assert loud > -30 and quiet < loud - 12  # the second track follows the first, and is quieter
    assert level(result.output, 7.2, 0.7) < -60  # the music is over, silence to the end
    info = probe_file(result.output)
    assert info.audio.duration == pytest.approx(8.0, abs=0.1) and info.video.duration == pytest.approx(8.0, abs=0.1)


def test_a_negative_shift_skips_the_beginning_of_the_music(media, library, tmp_path):
    plan = music_plan(MusicTrack("loud.wav", (MusicPart(0.0, 3.0),)), MusicTrack("quiet.wav", (MusicPart(0.0, 3.0),)),
                      offset=-2.0)
    result = play(plan, media, library, tmp_path)
    assert level(result.output, 0.1, 0.7) > -30  # one second of the first track is left
    assert level(result.output, 1.3, 2.4) < -35  # then the quiet one
    assert level(result.output, 4.5, 2.0) < -60


def test_a_muted_part_takes_its_time_in_silence(media, library, tmp_path):
    parts = (MusicPart(0.0, 2.0), MusicPart(2.0, 4.0, muted=True), MusicPart(4.0, 6.0))
    result = play(music_plan(MusicTrack("loud.wav", parts)), media, library, tmp_path)
    assert level(result.output, 0.2, 1.6) > -30 and level(result.output, 4.2, 1.6) > -30
    assert level(result.output, 2.2, 1.6) < -60
    assert level(result.output, 6.2, 1.6) < -60


def test_a_part_fades_in_and_out_by_itself(media, library, tmp_path):
    plan = music_plan(MusicTrack("loud.wav", (MusicPart(0.0, 6.0, False, 1.5, 1.5),)))
    result = play(plan, media, library, tmp_path)
    middle = level(result.output, 2.0, 2.0)
    assert middle > -30 and level(result.output, 0.0, 0.25) < middle - 12 and level(result.output, 5.75, 0.25) < middle - 12


def test_music_beyond_the_end_of_the_video_is_cut_with_the_fade_out_of_the_last_clip(media, library, tmp_path):
    plan = music_plan(MusicTrack("long.wav", (MusicPart(0.0, 30.0),)), elements=[clip("red4.mp4", fx(FADE_OUT, 2.5, 1.5))])
    result = play(plan, media, library, tmp_path)
    info = probe_file(result.output)
    assert info.audio.duration == pytest.approx(4.0, abs=0.1) and info.video.duration == pytest.approx(4.0, abs=0.1)
    middle = level(result.output, 1.0, 1.0)
    assert middle > -30 and level(result.output, 3.8, 0.2) < middle - 12
    assert result.report["music"]["fade_out"] == pytest.approx(1.5)


def test_without_a_fade_out_effect_the_music_ends_with_the_default_fade_out(media, library, tmp_path):
    plan = music_plan(MusicTrack("long.wav", (MusicPart(0.0, 30.0),)), elements=[clip("red4.mp4")])
    result = play(plan, media, library, tmp_path, config=RenderConfig(fade_in_seconds=0, fade_out_seconds=2.0))
    assert level(result.output, 3.8, 0.2) < level(result.output, 1.0, 1.0) - 12
    assert result.report["music"]["fade_out"] == pytest.approx(2.0)
    assert close(color_at(result.output, 3.95), PURE_RED)  # the picture does not fade: only an effect does that


def test_the_report_describes_the_music_of_the_plan_without_host_paths(media, library, tmp_path):
    plan = music_plan(MusicTrack("loud.wav", (MusicPart(0.0, 3.0), MusicPart(4.0, 6.0))), offset=0.5)
    music = play(plan, media, library, tmp_path).report["music"]
    assert music["mode"] == "plan" and music["tracks"] == ["loud.wav"] and music["parts"] == 2
    assert music["offset"] == 0.5
    assert str(library) not in json.dumps(music)


def test_a_part_cut_short_by_its_track_is_reported_as_a_warning(media, library, tmp_path, caplog):
    plan = music_plan(MusicTrack("loud.wav", (MusicPart(8.0, 14.0),)))
    with caplog.at_level("WARNING"):
        result = play(plan, media, library, tmp_path)
    assert "ends after the track" in caplog.text
    assert any("ends after the track" in warning for warning in result.report["warnings"])


def test_a_missing_track_stops_the_render_before_any_work(media, library, tmp_path):
    from vacation_memories.core import exit_codes
    from vacation_memories.rendering.renderer import RenderError

    plan = music_plan(MusicTrack("gone.mp3", (MusicPart(0.0, 3.0),)))
    with pytest.raises(RenderError, match="gone.mp3") as error:
        render(plan, media, tmp_path, music_dir=library)
    assert error.value.exit_code == exit_codes.USAGE
    assert list((tmp_path / "work" / "segments").glob("*")) == []


def test_the_music_option_still_plays_a_plan_without_a_music_block(media, library, tmp_path):
    config = RenderConfig(fade_in_seconds=DEFAULT_FADE_IN_SECONDS, fade_out_seconds=1.5)
    result = render(make_plan([clip("red4.mp4")]), media, tmp_path, music=library / "long.wav", config=config)
    assert result.ok and result.report["music"]["mode"] == "trimmed"
    assert level(result.output, 0.0, 0.2) < level(result.output, 1.5, 1.0) - 6
