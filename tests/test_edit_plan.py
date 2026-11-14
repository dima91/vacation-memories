import copy
import json
import os
from datetime import datetime, timezone
from fractions import Fraction

import pytest

from vacation_memories.core.config import ConfigError
from vacation_memories.planning.edit_plan import (
    HISTORY_DIR,
    HISTORY_KEEP,
    SCHEMA_VERSION,
    EdgeFades,
    EditPlan,
    Effect,
    Exclusion,
    MusicPart,
    MusicPlan,
    MusicTrack,
    OutputSpec,
    PlanElement,
    PlanError,
    add_edge_fades,
    back_up_plan,
    finalize_timeline,
    has_user_edits,
    keep_user_edits,
    load_plan,
    plan_from_dict,
    plan_layout,
    save_plan,
)
from vacation_memories.planning.transitions import (
    DISSOLVE,
    USER,
    TransitionRequest,
    TransitionRules,
    layout_timeline,
    seconds_to_frames,
)


def valid() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "requested_duration": 30,
        "planned_duration": 9.2,
        "output": {"width": 1920, "height": 1080, "fps": 30},
        "assumed_tz": "Europe/Rome",
        "runtime": {"jobs": 2},
        "elements": [
            {
                "id": "day1/IMG_0001.jpg", "origin": "planner",
                "source": "day1/IMG_0001.jpg", "type": "photo", "duration": 4.0, "duration_origin": "planner",
                "score": {"final": 0.8, "technical_score": 0.9, "narrative_score": 0.7, "components": {"faces": 1}},
                "moment_type": "scenery", "category": "beach", "section": "opening",
                "description": "the beach", "reason": "best of its group", "timestamp": 0.0,
                "effects": [{"id": "fx-1", "type": "fade_in", "offset": 0, "duration": 1.0, "origin": "planner"}],
            },
            {
                "id": "day1/VID_0001.mp4#1.000-7.000", "origin": "user",
                "source": "day1/VID_0001.mp4", "type": "video", "start": 1.0, "end": 7.0, "duration": 6.0,
                "effects": [{"id": "fx-2", "type": "dissolve", "offset": 0, "duration": 0.8, "origin": "planner"}],
                "timestamp": 3.2,
            },
        ],
        "excluded": [{"source": "day1/IMG_0009.jpg", "start": None, "end": None, "reason": "user"}],
        "music": {
            "tracks": [{"file": "calm/song.mp3", "parts": [
                {"start": 0, "end": 30, "muted": False, "fade_in": 2, "fade_out": 0},
                {"start": 40, "end": 60, "muted": True, "fade_in": 0, "fade_out": 3},
            ]}],
            "offset": -1.5,
        },
    }


def legacy() -> dict:
    """A schema 1 plan, as the previous version wrote it."""
    return {
        "schema_version": 1,
        "requested_duration": 30,
        "planned_duration": 9.2,
        "output": {"width": 1920, "height": 1080, "fps": 30},
        "elements": [
            {"source": "a.jpg", "type": "photo", "duration": 4.0, "transition_in": {"type": "cut", "duration": 0}},
            {"source": "b.mp4", "type": "video", "start": 1.0, "end": 7.0, "duration": 6.0,
             "transition_in": {"type": "dissolve", "duration": 0.8}, "timestamp": 3.2},
        ],
    }


def test_a_valid_plan_loads_with_all_its_fields():
    plan = plan_from_dict(valid())
    assert plan.schema_version == 2
    assert plan.output == OutputSpec(1920, 1080, 30.0)
    assert plan.requested_duration == 30 and plan.planned_duration == 9.2
    assert plan.assumed_tz == "Europe/Rome" and plan.runtime == {"jobs": 2}
    photo, video = plan.elements
    assert (photo.kind, photo.duration, photo.start, photo.end) == ("photo", 4.0, None, None)
    assert photo.moment_type == "scenery" and photo.section == "opening" and photo.score["final"] == 0.8
    assert (photo.id, photo.origin, photo.duration_origin) == ("day1/IMG_0001.jpg", "planner", "planner")
    assert photo.effects == (Effect("fx-1", "fade_in", 0.0, 1.0, "planner"),)
    assert (video.kind, video.start, video.end, video.duration) == ("video", 1.0, 7.0, 6.0)
    assert video.origin == "user" and video.duration_origin is None
    assert video.transition_in == TransitionRequest(DISSOLVE, 0.8)
    assert plan.excluded == (Exclusion("day1/IMG_0009.jpg"),)
    assert plan.music == MusicPlan(
        (MusicTrack("calm/song.mp3", (MusicPart(0, 30, False, 2, 0), MusicPart(40, 60, True, 0, 3))),), -1.5)


def test_round_trip_through_a_file(tmp_path):
    plan = plan_from_dict(valid())
    path = tmp_path / "edit_plan.json"
    save_plan(plan, path)
    assert load_plan(path) == plan
    assert json.loads(path.read_text())["schema_version"] == SCHEMA_VERSION


def test_a_hand_written_minimal_plan_is_enough():
    plan = plan_from_dict({
        "schema_version": 2,
        "output": {"width": 1280, "height": 720},
        "elements": [{"source": "a.mp4", "type": "video", "start": 0, "end": 2},
                     {"source": "b.jpg", "type": "photo", "duration": 3}],
    })
    video, photo = plan.elements
    assert plan.output.fps == 30.0 and video.duration == 2.0
    assert video.transition_in == TransitionRequest()
    assert (video.id, video.origin, video.effects) == ("a.mp4#0.000-2.000", "planner", ())
    assert (photo.id, photo.duration_origin) == ("b.jpg", "planner")
    assert plan.excluded == () and plan.music is None


def test_the_plan_without_a_music_block_differs_from_one_with_no_tracks(tmp_path):
    data = valid()
    del data["music"]
    assert plan_from_dict(data).music is None  # the music of --music, if any
    data["music"] = {"tracks": []}
    assert plan_from_dict(data).music == MusicPlan()  # a video without music
    path = tmp_path / "plan.json"
    save_plan(plan_from_dict(data), path)
    assert json.loads(path.read_text())["music"] == {"tracks": [], "offset": 0.0}


def test_unsupported_schema_version_is_rejected_with_a_clear_error():
    data = valid()
    data["schema_version"] = 99
    with pytest.raises(PlanError, match="unsupported schema_version 99"):
        plan_from_dict(data)
    del data["schema_version"]
    with pytest.raises(PlanError, match="unsupported schema_version None"):
        plan_from_dict(data)
    data["schema_version"] = True
    with pytest.raises(PlanError, match="unsupported schema_version True"):
        plan_from_dict(data)


def test_plan_error_is_a_usage_error():
    assert issubclass(PlanError, ConfigError)  # exit code 2


def mutate(change, base=valid) -> dict:
    data = copy.deepcopy(base())
    change(data)
    return data


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d.update(typo=1), "unknown key"),
        (lambda d: d["elements"][0].update(durations=3), "unknown key"),
        (lambda d: d["elements"][0].update(transition_in={"type": "cut"}), "unknown key.*transition_in"),
        (lambda d: d["elements"][0].update(type="audio"), "'type' must be 'photo' or 'video'"),
        (lambda d: d["elements"][0].update(duration=0), "'duration'"),
        (lambda d: d["elements"][0].update(duration="3"), "must be a number"),
        (lambda d: d["elements"][0].update(duration=True), "must be a number"),
        (lambda d: d["elements"][0].update(start=1.0), "a photo has no 'start' or 'end'"),
        (lambda d: d["elements"][0].pop("duration"), "a photo needs a 'duration'"),
        (lambda d: d["elements"][1].pop("end"), "a video needs 'end'"),
        (lambda d: d["elements"][1].update(end=1.0), "'end'"),
        (lambda d: d["elements"][1].update(start=-1), "'start'"),
        (lambda d: d["elements"][1].update(duration=2.0), "does not match end - start"),
        (lambda d: d["elements"][1].update(source="/etc/passwd"), "relative path inside the input folder"),
        (lambda d: d["elements"][1].update(source="../outside.mp4"), "relative path inside the input folder"),
        (lambda d: d["elements"][1].update(source=""), "non-empty path"),
        (lambda d: d["elements"][0].update(section=3), "'section' must be a string"),
        (lambda d: d["elements"][0]["score"].update(final="high"), "'score.final'"),
        (lambda d: d["elements"][0].update(score=[1]), "'score' must be an object"),
        (lambda d: d["output"].update(width=1921), "even integer"),
        (lambda d: d["output"].update(height=0), "even integer"),
        (lambda d: d["output"].update(fps=0), "'output.fps'"),
        (lambda d: d["output"].update(fps=1000), "at most"),
        (lambda d: d.pop("output"), "'output' must be an object"),
        (lambda d: d.update(elements={}), "'elements' must be a list"),
        (lambda d: d.update(runtime=[]), "'runtime' must be an object"),
        (lambda d: d.update(planned_duration="x"), "planned_duration"),
        # schema 2: ids, origins, effects
        (lambda d: d["elements"][1].update(id=""), "'id' must be a non-empty string"),
        (lambda d: d["elements"][1].update(id=7), "'id' must be a non-empty string"),
        (lambda d: d["elements"][1].update(id=d["elements"][0]["id"]), r"elements\[1\].*already used by elements\[0\]"),
        (lambda d: d["elements"][1].update(origin="robot"), "'origin' must be one of planner, user"),
        (lambda d: d["elements"][0].update(duration_origin="robot"), "'duration_origin' must be one of"),
        (lambda d: d["elements"][1].update(duration_origin="user"), "a video has no 'duration_origin'"),
        (lambda d: d["elements"][1].update(effects={}), "'effects' must be a list"),
        (lambda d: d["elements"][1]["effects"].append(5), r"effects\[1\] must be an object"),
        (lambda d: d["elements"][1]["effects"][0].update(type="wipe"), "'type' must be one of fade_in"),
        (lambda d: d["elements"][1]["effects"][0].update(duration=0), "'duration' must be greater than 0"),
        (lambda d: d["elements"][1]["effects"][0].update(offset=-1), "'offset' must be at least 0"),
        (lambda d: d["elements"][1]["effects"][0].update(origin="x"), "'origin' must be one of"),
        (lambda d: d["elements"][1]["effects"][0].update(speed=2), "unknown key"),
        (lambda d: d["elements"][1]["effects"][0].pop("id"), "'id' must be a non-empty string"),
        (lambda d: d["elements"][1]["effects"][0].update(id="fx-1"), "effect id 'fx-1' is already used in elements"),
        (lambda d: d["elements"][1]["effects"].append(
            {"id": "fx-3", "type": "dissolve", "offset": 0, "duration": 0.5}), "at most one dissolve"),
        # schema 2: excluded and music
        (lambda d: d.update(excluded={}), "'excluded' must be a list"),
        (lambda d: d["excluded"][0].update(reason="planner"), "'reason' must be 'user'"),
        (lambda d: d["excluded"][0].update(start=3), "both given .* or both null"),
        (lambda d: d["excluded"][0].update(start=3, end=2), "'end'"),
        (lambda d: d["excluded"][0].update(source="/abs.jpg"), "relative path inside the input folder"),
        (lambda d: d["excluded"][0].update(extra=1), "unknown key"),
        (lambda d: d.update(music=[]), "'music' must be an object"),
        (lambda d: d["music"].update(tracks={}), "'music.tracks' must be a list"),
        (lambda d: d["music"].update(offset="late"), "'music.offset' must be a number"),
        (lambda d: d["music"]["tracks"][0].update(file="/music/a.mp3"), "relative path inside the music folder"),
        (lambda d: d["music"]["tracks"][0].update(file="../a.mp3"), "relative path inside the music folder"),
        (lambda d: d["music"]["tracks"][0].update(parts=[]), "'parts' must be a non-empty list"),
        (lambda d: d["music"]["tracks"][0]["parts"][0].pop("end"), "a part needs 'end'"),
        (lambda d: d["music"]["tracks"][0]["parts"][0].update(end=0), "'end'"),
        (lambda d: d["music"]["tracks"][0]["parts"][0].update(muted="yes"), "'muted' must be true or false"),
        (lambda d: d["music"]["tracks"][0]["parts"][0].update(fade_in=-1), "'fade_in'"),
        (lambda d: d["music"].update(volume=2), "unknown key"),
    ],
)
def test_invalid_plans_are_rejected_with_the_place_of_the_problem(change, message):
    with pytest.raises(PlanError, match=message):
        plan_from_dict(mutate(change))


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d["elements"][1]["transition_in"].update(type="wipe"), "'transition_in.type'"),
        (lambda d: d["elements"][1]["transition_in"].update(duration=0), "needs a 'transition_in.duration'"),
        (lambda d: d["elements"][1]["transition_in"].update(duration=-1), "'transition_in.duration'"),
        (lambda d: d["elements"][1]["transition_in"].update(speed=2), "unknown key"),
        (lambda d: d["elements"][1].update(transition_in=[]), "'transition_in' must be an object"),
        (lambda d: d["elements"][1].update(effects=[]), "unknown key.*effects"),
        (lambda d: d["elements"][1].update(id="x"), "unknown key.*id"),
        (lambda d: d.update(music={"tracks": []}), "unknown key.*music"),
        (lambda d: d.update(excluded=[]), "unknown key.*excluded"),
    ],
)
def test_a_schema_1_plan_is_still_validated_as_schema_1(change, message):
    with pytest.raises(PlanError, match=message):
        plan_from_dict(mutate(change, legacy))


def test_the_error_names_the_element():
    data = mutate(lambda d: d["elements"][1].update(end=0.5))
    with pytest.raises(PlanError, match=r"elements\[1\]"):
        plan_from_dict(data)


def test_the_root_must_be_an_object():
    with pytest.raises(PlanError, match="JSON object"):
        plan_from_dict([])


def test_load_plan_reports_missing_and_malformed_files(tmp_path):
    with pytest.raises(PlanError, match="plan not found"):
        load_plan(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(PlanError, match="not valid JSON"):
        load_plan(bad)
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema_version": 7}))
    with pytest.raises(PlanError, match="invalid plan .*unsupported schema_version 7"):
        load_plan(wrong)


def test_non_utf8_file_names_survive_the_round_trip(tmp_path):
    name = os.fsdecode(b"day/caf\xe9.jpg")  # not valid UTF-8: kept as surrogate escapes
    plan = EditPlan(OutputSpec(320, 180), [PlanElement(name, "photo", 2.0)])
    path = tmp_path / "plan.json"
    save_plan(plan, path)
    assert path.read_bytes().isascii()
    loaded = load_plan(path)
    assert os.fsencode(loaded.elements[0].source) == b"day/caf\xe9.jpg"
    assert loaded == plan


def test_save_is_atomic_and_leaves_no_temporary_file(tmp_path):
    save_plan(plan_from_dict(valid()), tmp_path / "edit_plan.json")
    assert [p.name for p in tmp_path.iterdir()] == ["edit_plan.json"]


def test_fractional_frame_rates_are_exact():
    assert OutputSpec(320, 180, 29.97).frame_rate == Fraction(30000, 1001)
    assert OutputSpec(320, 180, 23.976).frame_rate == Fraction(24000, 1001)
    assert OutputSpec(320, 180, 59.94).frame_rate == Fraction(60000, 1001)
    assert OutputSpec(320, 180, 24).frame_rate == 24
    assert OutputSpec(320, 180, 25).frame_rate == 25
    assert OutputSpec(320, 180, 12.5).frame_rate == Fraction(25, 2)


# --- migration from schema 1 --------------------------------------------------------------------------------


class TestMigration:
    def test_transition_in_becomes_a_dissolve_effect_of_the_incoming_clip(self):
        plan = plan_from_dict(legacy())
        photo, video = plan.elements
        assert plan.schema_version == 2
        dissolve = [effect for effect in video.effects if effect.type == "dissolve"]
        assert dissolve == [Effect("fx-1", "dissolve", 0.0, 0.8, "planner")]  # origin planner: the 25% limit stays
        assert video.transition_in == TransitionRequest(DISSOLVE, 0.8)
        assert photo.transition_in == TransitionRequest()
        assert plan.excluded == () and plan.music is None
        assert (photo.origin, video.origin, photo.duration_origin) == ("planner", "planner", "planner")

    def test_ids_come_from_the_source_and_the_extent(self):
        photo, video = plan_from_dict(legacy()).elements
        assert photo.id == "a.jpg"
        assert video.id == "b.mp4#1.000-7.000"

    def test_a_clip_that_appears_twice_gets_unique_ids(self):
        data = legacy()
        data["elements"] += [copy.deepcopy(data["elements"][0]), copy.deepcopy(data["elements"][0])]
        ids = [element.id for element in plan_from_dict(data).elements]
        assert ids == ["a.jpg", "b.mp4#1.000-7.000", "a.jpg~2", "a.jpg~3"]
        assert len(set(ids)) == 4

    def test_the_global_fades_become_the_effects_of_the_first_and_last_clip(self):
        photo, video = plan_from_dict(legacy()).elements
        assert [(e.type, e.offset, e.duration) for e in photo.effects] == [("fade_in", 0.0, 1.0)]
        assert [(e.type, e.offset, e.duration) for e in video.effects if e.type != "dissolve"] == [
            ("fade_out", 3.0, 3.0)]
        assert all(effect.origin == "planner" for element in (photo, video) for effect in element.effects)

    def test_the_fades_have_the_configuration_values(self):
        photo, video = plan_from_dict(legacy(), fades=EdgeFades(0.5, 2.0)).elements
        assert [e.duration for e in photo.effects] == [0.5]
        assert [e.duration for e in video.effects if e.type == "fade_out"] == [2.0]
        none = plan_from_dict(legacy(), fades=EdgeFades(0.0, 0.0))
        assert [e.type for element in none.elements for e in element.effects] == ["dissolve"]

    def test_effect_ids_are_unique_in_the_plan(self):
        data = legacy()
        data["elements"] += [
            {"source": "c.jpg", "type": "photo", "duration": 4, "transition_in": {"type": "dissolve", "duration": 0.8}},
        ]
        plan = plan_from_dict(data)
        ids = [effect.id for element in plan.elements for effect in element.effects]
        assert len(ids) == len(set(ids)) == 4  # fade in, two dissolves, fade out

    def test_the_dissolve_into_the_first_clip_is_dropped(self):
        data = legacy()
        data["elements"][0]["transition_in"] = {"type": "dissolve", "duration": 0.8}
        assert plan_from_dict(data).elements[0].transition_in == TransitionRequest()

    def test_the_layout_and_the_duration_are_the_same_as_in_schema_1(self):
        data = legacy()
        data["elements"].append({"source": "c.mp4", "type": "video", "start": 0, "end": 1.2,
                                 "transition_in": {"type": "dissolve", "duration": 0.8}})
        data["elements"].append({"source": "d.jpg", "type": "photo", "duration": 3.5,
                                 "transition_in": {"type": "dissolve", "duration": 5.0}})  # beyond the 25% limit
        plan = plan_from_dict(data)
        rules, fps = TransitionRules(), plan.output.frame_rate
        frames = [seconds_to_frames(e["duration"] if e["type"] == "photo" else e["end"] - e["start"], fps)
                  for e in data["elements"]]
        requests = [TransitionRequest(e["transition_in"]["type"], e["transition_in"]["duration"])
                    for e in data["elements"]]
        expected = layout_timeline(frames, requests, fps, rules)
        got = plan_layout(plan, rules)
        assert [p.start_frame for p in got.placements] == [p.start_frame for p in expected.placements]
        assert [p.dissolve_frames for p in got.placements] == [p.dissolve_frames for p in expected.placements]
        assert got.duration == expected.duration

    def test_the_migrated_plan_is_written_as_schema_2(self, tmp_path):
        path = tmp_path / "edit_plan.json"
        path.write_text(json.dumps(legacy()))
        plan = load_plan(path)
        save_plan(plan, path)
        written = json.loads(path.read_text())
        assert written["schema_version"] == 2
        assert "transition_in" not in written["elements"][1]
        assert written["elements"][1]["id"] == "b.mp4#1.000-7.000"
        assert load_plan(path) == plan  # and it reads back as it is

    def test_a_migrated_plan_that_is_finalized_keeps_its_dissolves(self):
        final, _ = finalize_timeline(plan_from_dict(legacy()))
        assert final.elements[1].transition_in.duration == pytest.approx(0.8)

    def test_a_short_last_clip_gets_a_shorter_fade_out_that_does_not_overlap_the_dissolve(self):
        data = legacy()
        data["elements"][1].update(start=0.0, end=2.0, duration=2.0)
        plan = plan_from_dict(data)
        video = plan.elements[1]
        fade = next(effect for effect in video.effects if effect.type == "fade_out")
        assert fade.offset == pytest.approx(0.8) and fade.duration == pytest.approx(1.2)  # after the 0.8 s dissolve


class TestEdgeFades:
    def photo(self, duration, *effects, name="a.jpg"):
        return PlanElement(name, "photo", duration, effects=effects)

    def test_the_fade_in_is_shortened_by_the_dissolve_into_the_next_clip(self):
        first = self.photo(1.2)
        second = self.photo(4.0, Effect("fx-1", "dissolve", 0.0, 0.5), name="b.jpg")
        fades = add_edge_fades([first, second], EdgeFades(1.0, 0.0))
        assert [(e.type, e.offset, e.duration) for e in fades[0].effects] == [("fade_in", 0.0, pytest.approx(0.7))]

    def test_a_fade_below_the_minimum_is_left_out(self):
        first = self.photo(1.0)
        second = self.photo(4.0, Effect("fx-1", "dissolve", 0.0, 0.9), name="b.jpg")
        assert add_edge_fades([first, second], EdgeFades(1.0, 0.0))[0].effects == ()

    def test_both_fades_of_a_single_clip_share_it(self):
        (only,) = add_edge_fades([self.photo(3.0)], EdgeFades(1.0, 3.0))
        assert [(e.type, e.offset, e.duration) for e in only.effects] == [("fade_in", 0.0, 1.0), ("fade_out", 1.0, 2.0)]

    def test_an_existing_fade_is_not_added_twice(self):
        first = self.photo(4.0, Effect("fx-1", "fade_in", 0.0, 0.4, "user"))
        (only,) = add_edge_fades([first], EdgeFades(1.0, 0.0))
        assert only.effects == (Effect("fx-1", "fade_in", 0.0, 0.4, "user"),)

    def test_no_elements_no_fades(self):
        assert add_edge_fades([], EdgeFades()) == []


# --- finalize ---------------------------------------------------------------------------------------------------


def test_finalize_fills_timestamps_effective_transitions_and_planned_duration():
    plan = plan_from_dict(valid())
    final, layout = finalize_timeline(plan)
    photo, video = final.elements
    assert photo.timestamp == 0.0
    assert video.transition_in.type == DISSOLVE
    assert video.transition_in.duration == pytest.approx(0.8)
    assert video.timestamp == pytest.approx(4.0 - 0.8)
    assert final.planned_duration == pytest.approx(4.0 + 6.0 - 0.8)
    assert layout.duration == final.planned_duration


def test_finalize_reduces_a_transition_beyond_the_limits():
    data = valid()
    data["elements"][1]["effects"][0]["duration"] = 5.0
    final, _ = finalize_timeline(plan_from_dict(data))
    assert final.elements[1].transition_in.duration == pytest.approx(1.0)  # 25% of the 4 s photo
    assert final.elements[1].effects[0].id == "fx-2"  # the effect keeps its identity


def test_finalize_does_not_reduce_a_dissolve_of_the_user_to_a_quarter_of_the_clip():
    data = valid()
    data["elements"][1]["effects"][0].update(duration=2.5, origin="user")
    final, _ = finalize_timeline(plan_from_dict(data))
    assert final.elements[1].transition_in.duration == pytest.approx(2.5)
    assert final.planned_duration == pytest.approx(4.0 + 6.0 - 2.5)  # D2: the overlap is subtracted


def test_finalize_turns_a_too_short_dissolve_into_a_cut_and_uses_the_given_rules():
    data = valid()
    data["elements"][0]["duration"] = 0.7
    final, _ = finalize_timeline(plan_from_dict(data))
    assert final.elements[1].transition_in == TransitionRequest()
    assert final.elements[1].effects == ()  # a cut is no effect
    relaxed, _ = finalize_timeline(plan_from_dict(data), TransitionRules(min_seconds=0.1))
    assert relaxed.elements[1].transition_in.type == DISSOLVE


def test_finalize_keeps_the_other_effects():
    final, _ = finalize_timeline(plan_from_dict(valid()))
    assert final.elements[0].effects == plan_from_dict(valid()).elements[0].effects


def test_finalize_is_idempotent():
    once, _ = finalize_timeline(plan_from_dict(valid()))
    twice, _ = finalize_timeline(once)
    assert twice == once


# --- user edits and plan_history ----------------------------------------------------------------------------------


def plain_plan() -> EditPlan:
    plan = plan_from_dict(valid())
    return EditPlan(plan.output, [PlanElement("a.jpg", "photo", 3.0), PlanElement("b.mp4", "video", 4.0, 0.0, 4.0)])


class TestUserEdits:
    def test_a_plan_of_the_planner_has_none(self):
        assert not has_user_edits(plain_plan())

    @pytest.mark.parametrize(
        "change",
        [
            lambda plan: plan.elements.__setitem__(0, PlanElement("a.jpg", "photo", 3.0, origin="user")),
            lambda plan: plan.elements.__setitem__(0, PlanElement("a.jpg", "photo", 3.0, duration_origin="user")),
            lambda plan: plan.elements.__setitem__(
                1, PlanElement("b.mp4", "video", 4.0, 0.0, 4.0, effects=(Effect("fx-1", "dip", 1.0, 1.0, USER),))),
        ],
        ids=["element added by the user", "photo duration changed", "effect of the user"],
    )
    def test_each_kind_of_edit_counts(self, change):
        plan = plain_plan()
        change(plan)
        assert has_user_edits(plan)

    def test_exclusions_and_music_count(self):
        plan = plain_plan()
        assert has_user_edits(EditPlan(plan.output, plan.elements, excluded=(Exclusion("c.jpg"),)))
        assert has_user_edits(EditPlan(plan.output, plan.elements, music=MusicPlan()))


NOW = datetime(2026, 11, 14, 10, 0, 0, tzinfo=timezone.utc)


class TestRegeneration:
    def write(self, tmp_path, plan):
        path = tmp_path / "edit_plan.json"
        save_plan(plan, path)
        return path

    def edited(self) -> EditPlan:
        plan = plain_plan()
        return EditPlan(plan.output, plan.elements, excluded=(Exclusion("c.jpg"), Exclusion("d.mp4", 2.0, 5.0)))

    def test_no_plan_no_copy(self, tmp_path):
        assert keep_user_edits(tmp_path / "edit_plan.json") == ()
        assert not (tmp_path / HISTORY_DIR).exists()

    def test_a_plan_without_user_edits_is_not_copied(self, tmp_path):
        path = self.write(tmp_path, plain_plan())
        assert keep_user_edits(path) == ()
        assert not (tmp_path / HISTORY_DIR).exists()

    def test_a_schema_1_plan_is_not_copied(self, tmp_path):
        path = tmp_path / "edit_plan.json"
        path.write_text(json.dumps(legacy()))
        assert keep_user_edits(path) == ()
        assert not (tmp_path / HISTORY_DIR).exists()

    def test_a_plan_with_user_edits_is_copied_unchanged_and_its_exclusions_returned(self, tmp_path):
        path = self.write(tmp_path, self.edited())
        before = path.read_bytes()
        assert keep_user_edits(path, now=NOW) == (Exclusion("c.jpg"), Exclusion("d.mp4", 2.0, 5.0))
        (copy_,) = (tmp_path / HISTORY_DIR).iterdir()
        assert copy_.name == "edit_plan-20261114T100000000000Z.json"
        assert copy_.read_bytes() == before
        assert path.read_bytes() == before  # the caller overwrites it, not this function
        assert (tmp_path / HISTORY_DIR).stat().st_mode & 0o077 == 0

    def test_a_plan_that_cannot_be_read_is_copied_too(self, tmp_path):
        path = tmp_path / "edit_plan.json"
        path.write_text("{ hand edited, and broken")
        assert keep_user_edits(path, now=NOW) == ()
        (copy_,) = (tmp_path / HISTORY_DIR).iterdir()
        assert copy_.read_text() == "{ hand edited, and broken"

    def test_only_the_last_ten_copies_are_kept(self, tmp_path):
        path = self.write(tmp_path, self.edited())
        for minute in range(HISTORY_KEEP + 4):
            keep_user_edits(path, now=NOW.replace(minute=minute))
        names = sorted(p.name for p in (tmp_path / HISTORY_DIR).iterdir())
        assert len(names) == HISTORY_KEEP
        assert names[0] == "edit_plan-20261114T100400000000Z.json" and names[-1].startswith("edit_plan-20261114T101300")

    def test_two_copies_in_the_same_instant_do_not_overwrite_each_other(self, tmp_path):
        path = self.write(tmp_path, self.edited())
        keep_user_edits(path, now=NOW)
        keep_user_edits(path, now=NOW)
        assert len(list((tmp_path / HISTORY_DIR).iterdir())) == 2

    def test_if_the_copy_fails_the_plan_is_not_to_be_overwritten(self, tmp_path):
        path = self.write(tmp_path, self.edited())
        (tmp_path / HISTORY_DIR).write_text("a file where the folder should be")
        with pytest.raises(PlanError, match="cannot copy the plan"):
            keep_user_edits(path)

    def test_back_up_plan_copies_whatever_it_holds(self, tmp_path):
        assert back_up_plan(tmp_path / "edit_plan.json") is None
        path = self.write(tmp_path, plain_plan())
        copy_ = back_up_plan(path, now=NOW)
        assert copy_.read_bytes() == path.read_bytes()
