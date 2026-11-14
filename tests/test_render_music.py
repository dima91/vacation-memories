"""Which music a render plays: the plan's tracks and parts (checked before any work), or the `--music` file."""

import pytest
from render_fakes import music_file

from vacation_memories.core.process import ToolRunner
from vacation_memories.planning.edit_plan import MusicPart, MusicPlan, MusicTrack
from vacation_memories.rendering.music import PartSource, PartsMusic, TrackSource, load_music

RUNNER = ToolRunner()


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("music")
    music_file(root / "a.wav", 10.0)
    (root / "album").mkdir()
    music_file(root / "album" / "b.wav", 6.0, 880)
    return root


class Warnings(list):
    def __call__(self, message: str) -> None:
        self.append(message)


def load(library, music=None, track=None, **kwargs):
    warnings = Warnings()
    return load_music(music, track, library, RUNNER, warnings, **kwargs), warnings


def plan_of(*tracks: MusicTrack, offset: float = 0.0) -> MusicPlan:
    return MusicPlan(tuple(tracks), offset)


def test_the_tracks_of_the_plan_are_found_under_the_music_folder_and_keep_their_order(library):
    music = plan_of(
        MusicTrack("album/b.wav", (MusicPart(1.0, 4.0, False, 0.5, 0.5),)),
        MusicTrack("a.wav", (MusicPart(0.0, 3.0), MusicPart(5.0, 7.0, True))),
        offset=-1.5,
    )
    loaded, warnings = load(library, music)
    assert isinstance(loaded, PartsMusic) and warnings == []
    assert loaded.offset == -1.5
    assert loaded.parts == (
        PartSource(library / "album" / "b.wav", 1.0, 4.0, False, 0.5, 0.5),
        PartSource(library / "a.wav", 0.0, 3.0, False, 0.0, 0.0),
        PartSource(library / "a.wav", 5.0, 7.0, True, 0.0, 0.0),
    )
    assert loaded.files == ("album/b.wav", "a.wav")


def test_a_part_past_the_end_of_its_track_is_cut_at_the_end_with_a_warning(library):
    loaded, warnings = load(library, plan_of(MusicTrack("a.wav", (MusicPart(8.0, 15.0),))))
    assert loaded.parts[0].end == pytest.approx(10.0, abs=0.05)
    assert len(warnings) == 1 and "a.wav" in warnings[0] and "ends after the track" in warnings[0]


def test_a_part_that_starts_after_the_end_of_its_track_is_left_out_and_the_next_ones_move_up(library):
    music = plan_of(MusicTrack("a.wav", (MusicPart(0.0, 2.0), MusicPart(12.0, 14.0), MusicPart(3.0, 4.0))))
    loaded, warnings = load(library, music)
    assert [(part.start, part.end) for part in loaded.parts] == [(0.0, 2.0), (3.0, 4.0)]
    assert "left out" in warnings[0]


def test_fades_longer_than_their_part_are_scaled_down_together(library):
    loaded, warnings = load(library, plan_of(MusicTrack("a.wav", (MusicPart(0.0, 2.0, False, 3.0, 1.0),))))
    assert (loaded.parts[0].fade_in, loaded.parts[0].fade_out) == pytest.approx((1.5, 0.5))
    assert "fades" in warnings[0]


def test_a_plan_with_a_music_block_and_no_tracks_has_no_music(library):
    loaded, _ = load(library, MusicPlan(), track=library / "a.wav")
    assert loaded is None


def test_a_plan_with_tracks_ignores_the_music_option_and_says_so(library):
    loaded, warnings = load(library, plan_of(MusicTrack("a.wav", (MusicPart(0.0, 2.0),))), track=library / "a.wav")
    assert isinstance(loaded, PartsMusic) and "--music" in warnings[0]


def test_without_a_music_block_the_music_option_is_the_whole_track(library):
    loaded, warnings = load(library, None, track=library / "a.wav")
    assert isinstance(loaded, TrackSource) and warnings == []
    assert loaded.path == library / "a.wav" and loaded.duration == pytest.approx(10.0, abs=0.05)


def test_no_music_at_all(library):
    assert load(library)[0] is None


def test_a_missing_track_of_the_plan_is_an_error_naming_the_file(library):
    from vacation_memories.rendering.music import MusicError

    with pytest.raises(MusicError, match="gone.mp3"):
        load(library, plan_of(MusicTrack("gone.mp3", (MusicPart(0.0, 2.0),))))


def test_a_plan_with_music_needs_a_music_folder(library):
    from vacation_memories.rendering.music import MusicError

    with pytest.raises(MusicError, match="music folder"):
        load_music(plan_of(MusicTrack("a.wav", (MusicPart(0.0, 2.0),))), None, None, RUNNER, Warnings())


def test_a_track_that_is_not_audio_is_an_error(tmp_path):
    from vacation_memories.rendering.music import MusicError

    (tmp_path / "notes.mp3").write_text("not audio")
    with pytest.raises(MusicError, match="notes.mp3"):
        load_music(plan_of(MusicTrack("notes.mp3", (MusicPart(0.0, 2.0),))), None, tmp_path, RUNNER, Warnings())
