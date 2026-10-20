import copy
import json
import os
from fractions import Fraction

import pytest

from vacation_memories.edit_plan import (
    SCHEMA_VERSION,
    EditPlan,
    OutputSpec,
    PlanElement,
    PlanError,
    finalize_timeline,
    load_plan,
    plan_from_dict,
    save_plan,
)
from vacation_memories.transitions import DISSOLVE, TransitionRequest, TransitionRules


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
                "source": "day1/IMG_0001.jpg", "type": "photo", "duration": 4.0,
                "score": {"final": 0.8, "technical_score": 0.9, "narrative_score": 0.7, "components": {"faces": 1}},
                "moment_type": "scenery", "category": "beach", "section": "opening",
                "description": "the beach", "reason": "best of its group", "timestamp": 0.0,
                "transition_in": {"type": "cut", "duration": 0},
            },
            {
                "source": "day1/VID_0001.mp4", "type": "video", "start": 1.0, "end": 7.0, "duration": 6.0,
                "transition_in": {"type": "dissolve", "duration": 0.8}, "timestamp": 3.2,
            },
        ],
    }


def test_a_valid_plan_loads_with_all_its_fields():
    plan = plan_from_dict(valid())
    assert plan.output == OutputSpec(1920, 1080, 30.0)
    assert plan.requested_duration == 30 and plan.planned_duration == 9.2
    assert plan.assumed_tz == "Europe/Rome" and plan.runtime == {"jobs": 2}
    photo, video = plan.elements
    assert (photo.kind, photo.duration, photo.start, photo.end) == ("photo", 4.0, None, None)
    assert photo.moment_type == "scenery" and photo.section == "opening" and photo.score["final"] == 0.8
    assert (video.kind, video.start, video.end, video.duration) == ("video", 1.0, 7.0, 6.0)
    assert video.transition_in == TransitionRequest(DISSOLVE, 0.8)


def test_round_trip_through_a_file(tmp_path):
    plan = plan_from_dict(valid())
    path = tmp_path / "edit_plan.json"
    save_plan(plan, path)
    assert load_plan(path) == plan
    assert json.loads(path.read_text())["schema_version"] == SCHEMA_VERSION


def test_a_hand_written_minimal_plan_is_enough():
    plan = plan_from_dict({
        "schema_version": 1,
        "output": {"width": 1280, "height": 720},
        "elements": [{"source": "a.mp4", "type": "video", "start": 0, "end": 2}],
    })
    assert plan.output.fps == 30.0
    assert plan.elements[0].duration == 2.0
    assert plan.elements[0].transition_in == TransitionRequest()


def test_unsupported_schema_version_is_rejected_with_a_clear_error():
    data = valid()
    data["schema_version"] = 99
    with pytest.raises(PlanError, match="unsupported schema_version 99"):
        plan_from_dict(data)
    del data["schema_version"]
    with pytest.raises(PlanError, match="unsupported schema_version None"):
        plan_from_dict(data)


def test_plan_error_is_a_usage_error():
    from vacation_memories.config import ConfigError

    assert issubclass(PlanError, ConfigError)  # exit code 2


def mutate(change) -> dict:
    data = copy.deepcopy(valid())
    change(data)
    return data


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d.update(typo=1), "unknown key"),
        (lambda d: d["elements"][0].update(durations=3), "unknown key"),
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
        (lambda d: d["elements"][1]["transition_in"].update(type="wipe"), "'transition_in.type'"),
        (lambda d: d["elements"][1]["transition_in"].update(duration=0), "needs a 'transition_in.duration'"),
        (lambda d: d["elements"][1]["transition_in"].update(duration=-1), "'transition_in.duration'"),
        (lambda d: d["elements"][1]["transition_in"].update(speed=2), "unknown key"),
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
    ],
)
def test_invalid_plans_are_rejected_with_the_place_of_the_problem(change, message):
    with pytest.raises(PlanError, match=message):
        plan_from_dict(mutate(change))


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
    data["elements"][1]["transition_in"]["duration"] = 5.0
    final, _ = finalize_timeline(plan_from_dict(data))
    assert final.elements[1].transition_in.duration == pytest.approx(1.0)  # 25% of the 4 s photo


def test_finalize_turns_a_too_short_dissolve_into_a_cut_and_uses_the_given_rules():
    data = valid()
    data["elements"][0]["duration"] = 0.7
    final, _ = finalize_timeline(plan_from_dict(data))
    assert final.elements[1].transition_in == TransitionRequest()
    relaxed, _ = finalize_timeline(plan_from_dict(data), TransitionRules(min_seconds=0.1))
    assert relaxed.elements[1].transition_in.type == DISSOLVE


def test_finalize_is_idempotent():
    once, _ = finalize_timeline(plan_from_dict(valid()))
    twice, _ = finalize_timeline(once)
    assert twice == once
