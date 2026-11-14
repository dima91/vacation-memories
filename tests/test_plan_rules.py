"""The editing rules (`plan_rules`): limits, checks and duration, with their edge cases. No media involved."""

import json

import pytest

from vacation_memories.planning.edit_plan import (
    EditPlan,
    Effect,
    MusicPart,
    MusicPlan,
    MusicTrack,
    OutputSpec,
    PlanElement,
    PlanError,
    finalize_timeline,
    plan_from_dict,
    plan_layout,
)
from vacation_memories.planning.plan_rules import (
    MOMENT_MIN_SECONDS,
    MUSIC_MIN_AUDIBLE_SECONDS,
    PHOTO_MAX_SECONDS,
    PHOTO_MIN_SECONDS,
    check_plan,
    effect_room,
    ensure_valid,
    music_length,
    music_offset_limits,
    music_slots,
    photo_limits,
    plan_duration,
    plan_limits,
    remove_part,
    trim_limits,
)
from vacation_memories.planning.transitions import DISSOLVE, USER, TransitionRules, layout_timeline, seconds_to_frames

RULES = TransitionRules()
OUTPUT = OutputSpec(1280, 720, 30.0)


def photo(duration, *effects, name="p.jpg", origin="planner", duration_origin="planner"):
    return PlanElement(name, "photo", duration, effects=effects, origin=origin, duration_origin=duration_origin)


def moment(start, end, *effects, source="v.mp4", origin="planner"):
    return PlanElement(source, "video", end - start, start=start, end=end, effects=effects, origin=origin)


def effect(kind, offset, duration, identifier=None, origin="planner"):
    return Effect(identifier or f"{kind}-{offset:g}", kind, offset, duration, origin)


def dissolve(duration, origin="planner", identifier="d"):
    return Effect(identifier, DISSOLVE, 0.0, duration, origin)


def plan_of(*elements, music=None):
    return EditPlan(OUTPUT, list(elements), music=music)


def problems(plan, **kwargs):
    return [str(violation) for violation in check_plan(plan, RULES, **kwargs)]


# --- duration: one computation for the planner, the renderer and the editor ---------------------------------


class TestDuration:
    def test_clips_minus_the_overlaps_of_the_dissolves(self):
        plan = plan_of(photo(4.0), photo(4.0, dissolve(0.8)), moment(0.0, 5.0, dissolve(0.8, identifier="e")))
        assert plan_duration(plan, RULES) == pytest.approx(4 + 4 + 5 - 0.8 - 0.8)

    def test_an_empty_plan_lasts_nothing(self):
        assert plan_duration(plan_of(), RULES) == 0.0

    def test_it_is_what_the_layout_function_gives(self):
        plan = plan_of(photo(4.0), photo(3.0, dissolve(0.5)), moment(2.0, 9.0, dissolve(2.0, USER, "e")))
        fps = OUTPUT.frame_rate
        layout = layout_timeline(
            [seconds_to_frames(element.duration, fps) for element in plan.elements],
            [element.transition_in for element in plan.elements], fps, RULES)
        assert plan_duration(plan, RULES) == pytest.approx(layout.duration)

    def test_it_is_what_the_planner_writes_as_planned_duration(self):
        plan = plan_of(photo(4.0), photo(3.0, dissolve(0.5)), moment(2.0, 9.0, dissolve(2.0, USER, "e")))
        final, _ = finalize_timeline(plan, RULES)
        assert final.planned_duration == plan_duration(plan, RULES) == plan_duration(final, RULES)

    def test_a_dissolve_of_the_planner_is_limited_to_a_quarter_of_the_shorter_clip_and_one_of_the_user_is_not(self):
        planner = plan_of(photo(4.0), photo(4.0, dissolve(2.0, "planner")))
        user = plan_of(photo(4.0), photo(4.0, dissolve(2.0, "user")))
        assert plan_duration(planner, RULES) == pytest.approx(8 - 1.0)
        assert plan_duration(user, RULES) == pytest.approx(8 - 2.0)

    def test_a_dissolve_of_the_user_cannot_exceed_the_shorter_clip(self):
        user = plan_of(photo(4.0), photo(1.5, dissolve(3.0, "user")))
        assert plan_duration(user, RULES) == pytest.approx(4.0)  # the second clip is wholly inside the first

    def test_a_user_dissolve_below_the_minimum_is_a_cut(self):
        user = plan_of(photo(4.0), photo(4.0, dissolve(0.1, "user")))
        assert plan_duration(user, RULES) == pytest.approx(8.0)


# --- photos --------------------------------------------------------------------------------------------------------


class TestPhotoDuration:
    def test_the_limits_are_one_to_thirty_seconds_in_tenths(self):
        limits = photo_limits()
        assert (limits.minimum, limits.maximum, limits.step) == (PHOTO_MIN_SECONDS, PHOTO_MAX_SECONDS, 0.1)
        assert (PHOTO_MIN_SECONDS, PHOTO_MAX_SECONDS) == (1.0, 30.0)

    @pytest.mark.parametrize("seconds", [1.0, 1.1, 15.0, 29.9, 30.0])
    def test_inside_the_limits(self, seconds):
        assert problems(plan_of(photo(seconds, duration_origin="user"))) == []

    @pytest.mark.parametrize("seconds", [0.9, 0.1, 30.1, 90])
    def test_outside_the_limits(self, seconds):
        assert problems(plan_of(photo(seconds, duration_origin="user"))) == [
            f"elements[0].duration: a photo lasts between 1 and 30 s, got {seconds:g}"]

    def test_the_durations_of_the_planner_are_not_the_editors_business(self):
        assert problems(plan_of(photo(0.5), photo(45.0, name="q.jpg"))) == []


# --- moments --------------------------------------------------------------------------------------------------------


class TestMoments:
    def test_the_shortest_moment_lasts_one_second(self):
        assert MOMENT_MIN_SECONDS == 1.0
        assert problems(plan_of(moment(2.0, 3.0))) == []
        assert problems(plan_of(moment(2.0, 2.9))) == ["elements[0]: a moment lasts at least 1 s, got 0.9"]

    def test_a_moment_stays_inside_its_video(self):
        plan = plan_of(moment(8.0, 12.0))
        assert problems(plan, media_durations={"v.mp4": 12.0}) == []
        assert problems(plan, media_durations={"v.mp4": 11.5}) == [
            "elements[0].end: the moment ends after the video, which lasts 11.5 s"]
        assert problems(plan) == []  # an unknown length is not checked
        assert problems(plan, media_durations={"other.mp4": 3.0}) == []

    def test_moments_of_the_same_video_do_not_overlap_but_may_touch(self):
        assert problems(plan_of(moment(0.0, 4.0), moment(4.0, 8.0))) == []  # adjacent
        assert problems(plan_of(moment(0.0, 4.0), moment(3.0, 8.0))) == [
            "elements[0]: overlaps the moment elements[1] of the same video (3-8 s)"]
        assert problems(plan_of(moment(0.0, 4.0), moment(0.0, 4.0, source="other.mp4"))) == []

    def test_trim_limits_are_the_neighbors_and_the_video(self):
        elements = [moment(10.0, 14.0), moment(0.0, 4.0), moment(20.0, 25.0), moment(0.0, 9.0, source="other.mp4")]
        limits = trim_limits(elements, 0, media_duration=30.0)
        assert (limits.start_min, limits.end_max, limits.min_length) == (4.0, 20.0, 1.0)

    def test_the_video_bounds_the_last_moment_and_the_first_one_starts_at_zero(self):
        elements = [moment(10.0, 14.0), moment(20.0, 25.0)]
        assert trim_limits(elements, 1, 28.0).end_max == 28.0
        assert trim_limits(elements, 0, 28.0).start_min == 0.0
        assert trim_limits(elements, 1, None).end_max is None  # length unknown
        assert trim_limits(elements, 0, None).end_max == 20.0

    def test_a_moment_with_no_neighbors_may_use_the_whole_video(self):
        limits = trim_limits([moment(10.0, 14.0)], 0, 30.0)
        assert (limits.start_min, limits.end_max) == (0.0, 30.0)

    def test_touching_neighbors_leave_no_room_on_that_side(self):
        elements = [moment(4.0, 8.0), moment(0.0, 4.0), moment(8.0, 12.0)]
        limits = trim_limits(elements, 0, 30.0)
        assert (limits.start_min, limits.end_max) == (4.0, 8.0)


# --- the room for the effects ---------------------------------------------------------------------------------------


class TestEffectRoom:
    def clips(self, *first_effects, second=None):
        return [photo(5.0, *first_effects), second or photo(5.0, name="q.jpg")]

    def room(self, elements, index, kind, **options):
        return effect_room(elements, index, kind, RULES, **options)

    def test_a_clean_clip_has_all_its_length_free(self):
        elements = [photo(5.0)]
        assert self.room(elements, 0, "fade_in").windows == ((0.0, 5.0),)
        assert self.room(elements, 0, "fade_out").windows == ((0.0, 5.0),)
        assert self.room(elements, 0, "dip").windows == ((0.0, 5.0),)
        assert self.room(elements, 0, "fade_out").max_duration == 5.0

    def test_the_fade_in_stops_where_another_effect_starts(self):
        elements = [photo(5.0, effect("dip", 1.5, 1.0))]
        assert self.room(elements, 0, "fade_in").windows == ((0.0, 1.5),)

    def test_the_fade_out_starts_where_another_effect_ends(self):
        elements = [photo(5.0, effect("dip", 1.5, 1.0))]
        assert self.room(elements, 0, "fade_out").windows == ((2.5, 5.0),)

    def test_a_dip_goes_in_any_gap(self):
        elements = [photo(6.0, effect("fade_in", 0.0, 1.0), effect("dip", 3.0, 1.0), effect("fade_out", 4.5, 1.5))]
        assert self.room(elements, 0, "dip").windows == ((1.0, 3.0), (4.0, 4.5))

    def test_gaps_below_the_minimum_are_not_room(self):
        elements = [photo(6.0, effect("fade_in", 0.0, 1.0), effect("dip", 1.1, 1.0))]
        assert self.room(elements, 0, "dip").windows == ((2.1, 6.0),)  # the 0.1 s gap is too short
        assert self.room(elements, 0, "fade_in").windows == ()
        assert self.room(elements, 0, "fade_in").max_duration == 0.0

    def test_an_effect_that_is_moved_does_not_stand_in_its_own_way(self):
        elements = [photo(5.0, effect("fade_in", 0.0, 1.0, "fi"), effect("dip", 3.0, 1.0, "dp"))]
        assert self.room(elements, 0, "fade_in").windows == ()
        assert self.room(elements, 0, "fade_in", ignore="fi").windows == ((0.0, 3.0),)
        assert self.room(elements, 0, "dip", ignore="dp").windows == ((1.0, 5.0),)

    def test_a_dissolve_needs_the_free_space_of_both_clips(self):
        elements = self.clips(effect("dip", 2.0, 1.0))  # the first clip is free for its last 2 s
        assert self.room(elements, 1, "dissolve").windows == ((0.0, 2.0),)
        elements = self.clips(effect("dip", 2.0, 1.0), second=photo(5.0, effect("dip", 1.0, 1.0), name="q.jpg"))
        assert self.room(elements, 1, "dissolve").windows == ((0.0, 1.0),)  # the second one is free for 1 s only

    def test_a_clip_that_ends_with_a_fade_out_has_no_room_for_a_dissolve_into_the_next(self):
        elements = self.clips(effect("fade_out", 4.0, 1.0))
        assert self.room(elements, 1, "dissolve").windows == ()

    def test_a_dissolve_does_not_fit_in_the_shorter_clip(self):
        elements = [photo(5.0), photo(1.5, name="q.jpg")]
        assert self.room(elements, 1, "dissolve").max_duration == 1.5

    def test_there_is_nothing_to_dissolve_from_before_the_first_clip(self):
        assert self.room(self.clips(), 0, "dissolve").windows == ()

    def test_a_clip_has_room_for_one_dissolve_only(self):
        elements = self.clips(second=photo(5.0, dissolve(1.0), name="q.jpg"))
        assert self.room(elements, 1, "dissolve").windows == ()
        assert self.room(elements, 1, "dissolve", ignore="d").windows == ((0.0, 5.0),)

    def test_the_dissolve_into_the_next_clip_takes_the_end_of_this_one(self):
        elements = self.clips(second=photo(5.0, dissolve(1.0), name="q.jpg"))
        assert self.room(elements, 0, "fade_out").windows == ()
        assert self.room(elements, 0, "dip").windows == ((0.0, 4.0),)

    def test_no_room_at_the_end_of_a_clip_whose_next_clip_dissolves_over_all_of_it(self):
        elements = self.clips(second=photo(5.0, dissolve(5.0, USER), name="q.jpg"))
        assert self.room(elements, 0, "fade_out").windows == ()

    def test_an_unknown_effect_type_is_an_error(self):
        with pytest.raises(ValueError):
            self.room([photo(5.0)], 0, "wipe")


class TestEffectChecks:
    def one(self, *effects, duration=5.0):
        return plan_of(photo(duration, *effects))

    @pytest.mark.parametrize("kind", ["fade_in", "fade_out", "dip"])
    def test_a_good_effect_passes(self, kind):
        offset = {"fade_in": 0.0, "fade_out": 4.0, "dip": 2.0}[kind]
        assert problems(self.one(effect(kind, offset, 1.0))) == []

    def test_an_effect_stays_inside_its_clip(self):
        assert problems(self.one(effect("dip", 4.5, 1.0))) == [
            "elements[0].effects[0]: the dip does not fit in the clip, which lasts 5 s"]

    def test_fade_in_and_dissolve_start_at_the_start_of_the_clip(self):
        assert problems(self.one(effect("fade_in", 1.0, 1.0))) == [
            "elements[0].effects[0]: a fade_in starts at the start of the clip (offset 0)"]
        two = plan_of(photo(4.0), photo(4.0, Effect("d", DISSOLVE, 1.0, 1.0), name="q.jpg"))
        assert problems(two) == ["elements[1].effects[0]: a dissolve starts at the start of the clip (offset 0)"]

    def test_a_fade_out_ends_at_the_end_of_the_clip(self):
        assert problems(self.one(effect("fade_out", 1.0, 1.0))) == [
            "elements[0].effects[0]: a fade_out ends at the end of the clip"]

    def test_a_dip_can_be_anywhere(self):
        for offset in (0.0, 1.7, 4.0):
            assert problems(self.one(effect("dip", offset, 1.0))) == []

    def test_an_effect_of_the_user_lasts_at_least_two_tenths(self):
        assert problems(self.one(effect("dip", 1.0, 0.2, origin="user"))) == []
        assert problems(self.one(effect("dip", 1.0, 0.19, origin="user"))) == [
            "elements[0].effects[0]: an effect lasts at least 0.2 s, got 0.19"]
        assert problems(self.one(effect("dip", 1.0, 0.1, origin="planner"))) == []  # the planner has its own rules

    def test_effects_of_a_clip_do_not_overlap(self):
        plan = self.one(effect("fade_in", 0.0, 1.5, "a"), effect("dip", 1.0, 1.0, "b"))
        assert problems(plan) == ["elements[0].effects[0]: overlaps the dip effect 'b'"]
        assert problems(self.one(effect("fade_in", 0.0, 1.0, "a"), effect("dip", 1.0, 1.0, "b"))) == []  # they touch

    def test_the_dissolve_into_the_next_clip_counts_as_an_effect_of_this_one(self):
        plan = plan_of(photo(4.0, effect("fade_out", 2.0, 2.0, "out")), photo(4.0, dissolve(1.0, USER), name="q.jpg"))
        assert problems(plan) == ["elements[0].effects[0]: overlaps the dissolve into the next clip"]

    def test_a_dissolve_must_fit_in_both_clips(self):
        plan = plan_of(photo(4.0), photo(2.0, dissolve(3.0, USER), name="q.jpg"))
        assert problems(plan) == ["elements[1].effects[0]: the dissolve does not fit in the clip, which lasts 2 s"]
        plan = plan_of(photo(2.0), photo(4.0, dissolve(3.0, USER), name="q.jpg"))
        assert problems(plan) == ["elements[1].effects[0]: the dissolve does not fit in the previous clip, which lasts 2 s"]
        plan = plan_of(photo(4.0), photo(2.0, dissolve(2.0, USER), name="q.jpg"))
        assert problems(plan) == []  # exactly the shorter clip: it fits

    def test_a_dissolve_of_the_user_may_exceed_a_quarter_of_the_clip(self):
        assert problems(plan_of(photo(4.0), photo(4.0, dissolve(3.5, USER), name="q.jpg"))) == []

    def test_a_dissolve_of_the_planner_keeps_the_limit_of_a_quarter_of_the_shorter_clip(self):
        assert problems(plan_of(photo(4.0), photo(6.0, dissolve(1.0), name="q.jpg"))) == []
        assert problems(plan_of(photo(4.0), photo(6.0, dissolve(1.1), name="q.jpg"))) == [
            "elements[1].effects[0]: a dissolve of the planner lasts at most 25% of the shorter clip (1 s)"]

    def test_the_first_clip_has_nothing_to_dissolve_from(self):
        assert problems(plan_of(photo(4.0, dissolve(1.0)))) == [
            "elements[0].effects[0]: the first clip has no clip before it to dissolve from"]

    def test_a_dissolve_and_a_fade_in_cannot_both_start_a_clip(self):
        plan = plan_of(photo(4.0), photo(4.0, effect("fade_in", 0.0, 1.0, "fi"), dissolve(1.0, USER), name="q.jpg"))
        assert problems(plan) == ["elements[1].effects[0]: overlaps the dissolve effect 'd'"]


# --- music ----------------------------------------------------------------------------------------------------------


def track(file, *parts):
    return MusicTrack(file, tuple(MusicPart(start, end, *rest) for start, end, *rest in parts))


class TestMusic:
    def two_tracks(self, offset=0.0):
        return MusicPlan((track("a.mp3", (0, 10), (20, 25, True)), track("b.mp3", (5, 15))), offset)

    def test_the_parts_follow_one_another_on_the_axis_of_the_video(self):
        slots = music_slots(self.two_tracks())
        assert [(s.track, s.part, s.file, s.start, s.end, s.muted) for s in slots] == [
            (0, 0, "a.mp3", 0, 10, False), (0, 1, "a.mp3", 10, 15, True), (1, 0, "b.mp3", 15, 25, False)]
        assert music_length(self.two_tracks()) == 25

    def test_a_positive_offset_starts_the_music_later_and_a_negative_one_skips_its_beginning(self):
        assert [s.start for s in music_slots(self.two_tracks(offset=3.5))] == [3.5, 13.5, 18.5]
        assert [s.start for s in music_slots(self.two_tracks(offset=-4.0))] == [-4.0, 6.0, 11.0]  # the first part begins before 0

    def test_no_tracks_no_slots(self):
        assert music_slots(MusicPlan()) == [] and music_length(MusicPlan()) == 0

    def test_deleting_a_part_shifts_the_following_ones(self):
        music = remove_part(self.two_tracks(), 0, 0)
        assert [(s.file, s.start, s.end) for s in music_slots(music)] == [("a.mp3", 0, 5), ("b.mp3", 5, 15)]
        assert music.tracks[0].parts == (MusicPart(20, 25, True),)

    def test_deleting_the_last_part_of_a_track_removes_the_track(self):
        music = remove_part(self.two_tracks(), 1, 0)
        assert [t.file for t in music.tracks] == ["a.mp3"]
        assert music_length(music) == 15

    def test_deleting_the_only_part_leaves_a_video_without_music(self):
        music = remove_part(MusicPlan((track("a.mp3", (0, 10)),), 2.0), 0, 0)
        assert music.tracks == () and music.offset == 2.0 and music_slots(music) == []

    def test_deleting_a_part_does_not_touch_the_original(self):
        original = self.two_tracks()
        remove_part(original, 0, 0)
        assert len(original.tracks[0].parts) == 2

    def test_the_shift_leaves_at_least_a_second_of_music_in_the_video(self):
        limits = music_offset_limits(self.two_tracks(), 120.0)  # 25 s of music, a 120 s video
        assert (limits.minimum, limits.maximum, limits.step) == (-24.0, 119.0, 0.1)
        assert MUSIC_MIN_AUDIBLE_SECONDS == 1.0

    def test_the_limits_with_little_music_or_a_short_video(self):
        assert music_offset_limits(MusicPlan((track("a.mp3", (0, 0.5)),)), 60.0).minimum == 0.0
        assert music_offset_limits(MusicPlan((track("a.mp3", (0, 30)),)), 0.5).maximum == 0.0
        empty = music_offset_limits(MusicPlan(), 60.0)
        assert (empty.minimum, empty.maximum) == (0.0, 59.0)

    @pytest.mark.parametrize("offset, ok", [(-24.0, True), (-24.1, False), (0.0, True), (49.0, True), (49.1, False)])
    def test_the_shift_is_checked(self, offset, ok):
        plan = plan_of(photo(50.0), music=MusicPlan((track("a.mp3", (0, 25)),), offset))
        assert (problems(plan) == []) is ok
        if not ok:
            assert problems(plan)[0].startswith("music.offset: the shift of the music is between -24 and 49 s")

    def test_the_parts_of_a_track_are_in_order_and_do_not_overlap(self):
        plan = plan_of(photo(50.0), music=MusicPlan((track("a.mp3", (0, 10), (9, 20)),)))
        assert problems(plan) == ["music.tracks[0].parts[1]: the parts of a track are in order and do not overlap"]
        ok = plan_of(photo(50.0), music=MusicPlan((track("a.mp3", (0, 10), (10, 20), (30, 40)),)))
        assert problems(ok) == []

    def test_a_part_stays_inside_its_track(self):
        plan = plan_of(photo(50.0), music=MusicPlan((track("a.mp3", (0, 31)),)))
        assert problems(plan, track_durations={"a.mp3": 31.0}) == []
        assert problems(plan, track_durations={"a.mp3": 30.0}) == [
            "music.tracks[0].parts[0].end: the part ends after the track, which lasts 30 s"]
        assert problems(plan) == []

    def test_the_fades_of_a_part_fit_in_it(self):
        plan = plan_of(photo(50.0), music=MusicPlan((track("a.mp3", (0, 4, False, 2.5, 2.5)),)))
        assert problems(plan) == ["music.tracks[0].parts[0]: the fade-in and the fade-out do not fit in the part"]
        fine = plan_of(photo(50.0), music=MusicPlan((track("a.mp3", (0, 4, False, 2.0, 2.0)),)))
        assert problems(fine) == []


# --- limits of a whole plan, as sent to the interface -----------------------------------------------------------------


class TestPlanLimits:
    def plan(self):
        return plan_of(
            photo(4.0, effect("fade_in", 0.0, 1.0, "fi")),
            moment(10.0, 14.0, dissolve(0.8, identifier="ds")),
            moment(0.0, 5.0, source="other.mp4"),
            music=MusicPlan((track("a.mp3", (0, 30)),)),
        )

    def test_every_element_has_its_limits_by_id(self):
        limits = plan_limits(self.plan(), RULES, media_durations={"v.mp4": 40.0})
        assert list(limits.elements) == ["p.jpg", "v.mp4#10.000-14.000", "other.mp4#0.000-5.000"]
        photo_limits_ = limits.elements["p.jpg"]
        assert photo_limits_.trim is None and photo_limits_.duration.minimum == 1.0
        video = limits.elements["v.mp4#10.000-14.000"]
        assert video.duration is None and (video.trim.start_min, video.trim.end_max) == (0.0, 40.0)
        assert limits.elements["other.mp4#0.000-5.000"].trim.end_max is None  # its length is not known

    def test_every_effect_has_the_room_it_may_use_and_every_type_where_it_could_go(self):
        limits = plan_limits(self.plan(), RULES)
        first = limits.elements["p.jpg"]
        assert first.effects["fi"].windows == ((0.0, 3.2),)  # up to the dissolve into the next clip
        assert first.available["fade_in"].windows == ()  # there is one already
        assert first.available["dip"].windows == ((1.0, 3.2),)
        second = limits.elements["v.mp4#10.000-14.000"]
        assert second.effects["ds"].windows == ((0.0, 3.0),)  # the first clip has 3 s free at its end
        assert second.available["dissolve"].windows == ()

    def test_the_music_shift_is_part_of_the_limits(self):
        limits = plan_limits(self.plan(), RULES)
        assert limits.music_offset.minimum == -29.0
        assert limits.music_offset.maximum == pytest.approx(plan_duration(self.plan(), RULES) - 1.0)
        assert plan_limits(plan_of(photo(4.0)), RULES).music_offset is None

    def test_it_travels_as_json_next_to_the_plan(self):
        data = plan_limits(self.plan(), RULES, media_durations={"v.mp4": 40.0}).to_dict()
        assert json.loads(json.dumps(data)) == data
        assert data["min_effect_seconds"] == 0.2
        assert data["elements"]["p.jpg"]["duration"] == {"min": 1.0, "max": 30.0, "step": 0.1}
        assert data["elements"]["v.mp4#10.000-14.000"]["trim"] == {"start_min": 0.0, "end_max": 40.0, "min_length": 1.0}
        assert data["elements"]["p.jpg"]["effects"]["fi"] == {"type": "fade_in", "windows": [[0.0, 3.2]]}
        assert set(data["elements"]["p.jpg"]["available"]) == {"fade_in", "fade_out", "dissolve", "dip"}
        assert data["music"]["offset"]["min"] == -29.0

    def test_the_limits_are_not_written_into_the_plan(self):
        keys = set(self.plan().to_dict())
        assert "limits" not in keys
        assert not any("limit" in key or "room" in key for element in self.plan().to_dict()["elements"] for key in element)


# --- checks ---------------------------------------------------------------------------------------------------------------


class TestChecks:
    def test_a_plan_of_the_planner_passes(self):
        plan = plan_from_dict({
            "schema_version": 1, "output": {"width": 640, "height": 360},
            "elements": [
                {"source": "a.jpg", "type": "photo", "duration": 4},
                {"source": "b.jpg", "type": "photo", "duration": 4, "transition_in": {"type": "dissolve", "duration": 0.8}},
                {"source": "c.mp4", "type": "video", "start": 2, "end": 8,
                 "transition_in": {"type": "dissolve", "duration": 0.8}},
            ],
        })
        assert problems(plan) == []  # a migrated schema 1 plan, with its fades

    def test_every_problem_is_listed_with_its_place(self):
        plan = plan_of(photo(0.5, duration_origin="user"), moment(0.0, 0.5), photo(40.0, name="q.jpg", duration_origin="user"))
        assert [p.split(":")[0] for p in problems(plan)] == ["elements[0].duration", "elements[1]", "elements[2].duration"]

    def test_ensure_valid_names_the_first_problem_and_counts_the_others(self):
        plan = plan_of(photo(0.5, duration_origin="user"), photo(40.0, name="q.jpg", duration_origin="user"))
        with pytest.raises(PlanError, match=r"elements\[0\]\.duration: a photo lasts between 1 and 30 s, got 0\.5 \(and 1 more\)"):
            ensure_valid(plan, RULES)
        ensure_valid(plan_of(photo(4.0)), RULES)

    def test_a_plan_that_passes_checks_gives_the_same_layout_for_the_renderer_and_the_editor(self):
        plan = plan_of(photo(4.0), photo(4.0, dissolve(2.0, USER), name="q.jpg"), moment(0.0, 6.0, dissolve(1.0)))
        assert problems(plan) == []
        assert plan_layout(plan, RULES).duration == pytest.approx(plan_duration(plan, RULES))
