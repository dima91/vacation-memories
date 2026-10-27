from collections import defaultdict
from dataclasses import replace
from datetime import timedelta

import pytest
from selection_fakes import START, ranked, ranking_of, shot, vacation

from vacation_memories.edit_plan import OutputSpec, plan_from_dict
from vacation_memories.edit_planner import plan_edit
from vacation_memories.selection_config import SelectionConfig
from vacation_memories.transitions import TransitionRules

CONFIG = SelectionConfig()
OUTPUT = OutputSpec(1280, 720)
MOMENTS = ["scenery", "social", "action", "detail", "social", "action", "other"]
BODY = ("beginning", "development", "finale", "body", "chronological")


def plan(items, target, *, structure="narrative", config=CONFIG, ai_used=True):
    return plan_edit(ranking_of(items, ai_used=ai_used), target=target, output=OUTPUT, config=config,
                     rules=TransitionRules(), structure=structure)


def sections(result) -> dict:
    return {section["name"]: section for section in result.report["sections"]}


def elements_of(result, *names):
    return [element for element in result.plan.elements if element.section in names]


def assert_no_long_runs(result, limit=3):
    run, previous = 0, None
    for element in result.plan.elements:
        moment = element.moment_type
        run = run + 1 if moment is not None and moment == previous else 1
        previous = moment
        assert run <= limit, f"more than {limit} clips of type {moment} in a row"


def assert_chronological(elements):
    times = [element.captured_at for element in elements]
    assert times == sorted(times)


RIDE = "day2/ride.mp4"
RIDE_POINTS = (2, 9, 16, 23)  # the good moments of a 5-minute scene in 10 s windows


def ride(points=RIDE_POINTS, *, name=RIDE, strong=0.95, when=START + timedelta(days=1, hours=4), **options):
    return shot(name, when, [strong if n in points else 0.3 for n in range(30)], **options)


def pieces(result, source, *names):
    return [e for e in result.plan.elements if e.source == source and (not names or e.section in names)]


def assert_shots_are_continuous(result, items):
    """Every clip covers only segments of the ranking, contiguous in its file, and no source time is used twice."""
    windows = defaultdict(list)
    for item in items:
        if item.candidate.kind == "video":
            windows[item.candidate.rel_path.as_posix()].append((item.candidate.start, item.candidate.end))
    used = defaultdict(list)
    for element in result.plan.elements:
        if element.kind != "video":
            continue
        covering = sorted(w for w in windows[element.source] if w[0] < element.end and w[1] > element.start)
        assert covering[0][0] <= element.start + 1e-6 and covering[-1][1] >= element.end - 1e-6, element
        for (_, end), (start, _) in zip(covering, covering[1:]):
            assert start == pytest.approx(end), f"{element.source}: a gap between {end} and {start}"
        used[element.source].append((element.start, element.end))
    for source, spans in used.items():
        spans.sort()
        for (_, end), (start, _) in zip(spans, spans[1:]):
            assert start >= end - 1e-6, f"{source}: the same source time is used twice"


class TestNarrativeStructure:
    @pytest.mark.parametrize("weights", [None, [8, 1, 1]], ids=["uniform", "80% on one day"])
    def test_budgets_stay_in_their_ranges_and_add_up_to_the_duration(self, weights):
        result = plan(vacation(200, weights=weights, moments=MOMENTS), 300)
        assert result.report["structure"] == "narrative"
        found = sections(result)
        assert list(found) == ["opening", "beginning", "development", "finale", "closing"]
        for section in found.values():
            low, high = section["allowed_range"]
            assert low <= section["planned"] <= high, section
            assert not section["redistributed"]
        assert sum(s["planned"] for s in found.values()) == pytest.approx(300, rel=0.05)
        assert result.plan.planned_duration == pytest.approx(300, rel=0.05)
        assert result.plan.requested_duration == 300

    def test_with_300_seconds_the_initial_budgets_are_the_ones_of_the_specification(self):
        found = sections(plan(vacation(200, moments=MOMENTS), 300))
        assert [found[name]["initial_budget"] for name in found] == [30, 60, 135, 60, 15]

    @pytest.mark.parametrize("weights", [None, [8, 1, 1]], ids=["uniform", "80% on one day"])
    def test_the_bands_are_quantiles_of_the_material_not_of_the_calendar(self, weights):
        result = plan(vacation(200, weights=weights, moments=MOMENTS), 300)
        for name in ("beginning", "development", "finale"):
            assert sections(result)[name]["clips"] > 3
        # Beginning, Development and Finale follow each other in time.
        body = elements_of(result, "beginning", "development", "finale")
        assert_chronological(body)

    def test_opening_clips_are_never_repeated_later(self):
        result = plan(vacation(200, moments=MOMENTS), 300)
        opening = {(e.source, e.start) for e in elements_of(result, "opening")}
        later = [(e.source, e.start) for e in result.plan.elements if e.section != "opening"]
        assert opening and not opening & set(later)
        assert len(later) == len(set(later))

    def test_the_opening_shows_the_best_moments_in_time_order_with_quick_clips_and_no_transition(self):
        items = vacation(200, moments=MOMENTS + ["transition"])
        result = plan(items, 300)
        opening = elements_of(result, "opening")
        best = sorted(item.narrative_score for item in items if item.moment_type != "transition")[-len(opening):]
        assert min(e.score["narrative_score"] for e in opening) >= best[0] - 0.15
        assert all(e.duration <= CONFIG.opening_clip_max_seconds + 0.05 for e in opening)
        assert all(e.moment_type != "transition" for e in opening)
        assert len({e.captured_at[:10] for e in opening}) > 1  # variety of days
        assert_chronological(opening)

    def test_the_closing_is_a_calm_clip_with_the_longest_dissolve_its_neighbor_allows(self):
        items = vacation(200, moments=MOMENTS)
        result = plan(items, 300)
        closing = elements_of(result, "closing")
        assert closing and all(e.moment_type in ("scenery", "social") for e in closing)
        assert all(e.duration >= CONFIG.closing_photo_min_seconds - 0.05 for e in closing)
        previous = result.plan.elements[result.plan.elements.index(closing[0]) - 1]
        limit = min(CONFIG.closing_dissolve_seconds, TransitionRules().max_fraction * min(previous.duration, closing[0].duration))
        assert closing[0].transition_in.type == "dissolve"
        assert closing[0].transition_in.duration == pytest.approx(limit, abs=1 / 30)
        assert result.plan.elements[-1].section == "closing"

    def test_without_ai_the_closing_falls_back_on_calm_and_late_shots(self):
        items = vacation(200)
        result = plan(items, 300, ai_used=False)
        closing = elements_of(result, "closing")
        motion = {item.id: item.motion_level for item in items}
        assert closing and sum(motion[e.source] for e in closing) / len(closing) < 0.5
        assert sections(result)["closing"]["planned"] > 0

    def test_insufficient_material_in_a_section_is_redistributed_without_repeating_clips(self):
        # Only one serene shot: the Closing cannot fill its 15 s, the others take the rest.
        items = vacation(200, moments=["action", "detail", "other"])
        items.append(ranked("day3/sunset.jpg", START + timedelta(days=2, hours=11.9), moment="scenery", value=4,
                            narrative=0.2))
        result = plan(items, 300)
        found = sections(result)
        assert found["closing"]["clips"] == 1 and found["closing"]["ran_out_of_material"]
        assert found["closing"]["planned"] < 15
        assert any(found[name]["redistributed"] for name in ("opening", "beginning", "development", "finale"))
        assert result.plan.planned_duration == pytest.approx(300, rel=0.05)
        keys = [(e.source, e.start) for e in result.plan.elements]
        assert len(keys) == len(set(keys))

    def test_a_band_that_runs_out_is_compensated_by_the_others(self):
        # The last day is all weak transitions, which are never used: the Finale band has nothing to show.
        items = vacation(150, days=2, moments=MOMENTS)
        items += [ranked(f"day3/road{n:02d}.jpg", START + timedelta(days=2, minutes=10 * n), moment="transition",
                         value=2, narrative=0.3) for n in range(60)]
        result = plan(items, 300)
        found = sections(result)
        assert found["finale"]["ran_out_of_material"] and found["finale"]["planned"] < 60
        assert all(found[name]["redistributed"] for name in ("opening", "beginning", "development"))
        assert result.plan.planned_duration == pytest.approx(300, rel=0.05)
        keys = [(e.source, e.start) for e in result.plan.elements]
        assert len(keys) == len(set(keys))
        assert not any(e.source.startswith("day3/road") for e in result.plan.elements)

    def test_not_enough_material_gives_a_shorter_video_and_says_why(self):
        result = plan(vacation(20, moments=MOMENTS), 300)
        assert result.report["insufficient_material"]
        assert result.plan.planned_duration < 300 * 0.95
        assert any("insufficient material" in warning for warning in result.report["warnings"])
        keys = [(e.source, e.start) for e in result.plan.elements]
        assert len(keys) == len(set(keys)) >= 15

    def test_the_closing_always_gets_a_clip_paid_by_development(self):
        result = plan(vacation(200, moments=MOMENTS), 60)
        found = sections(result)
        assert found["closing"]["initial_budget"] == pytest.approx(CONFIG.closing_photo_min_seconds)
        assert found["development"]["initial_budget"] == pytest.approx(0.45 * 60 - (4 - 3))


class TestCompressedStructure:
    def test_short_targets_have_one_chronological_body(self):
        result = plan(vacation(100, moments=MOMENTS), 30)
        assert result.report["structure"] == "compressed"
        found = sections(result)
        assert list(found) == ["opening", "body", "closing"]
        assert found["opening"]["planned"] <= 5.05
        assert found["closing"]["clips"] == 1 and found["closing"]["planned"] >= 3 - 0.05
        assert_chronological(elements_of(result, "body"))
        assert result.plan.planned_duration == pytest.approx(30, rel=0.05)

    def test_short_targets_use_short_clips(self):
        result = plan(vacation(100, moments=MOMENTS), 45)
        body = elements_of(result, "body")
        assert all(element.duration <= 3.05 for element in body if element.kind == "photo")

    def test_scarce_material_also_compresses_the_structure(self):
        result = plan(vacation(12, moments=MOMENTS), 60)
        assert result.report["structure"] == "compressed"

    def test_at_the_minimum_duration_the_opening_is_merged_into_the_body(self):
        result = plan(vacation(100, moments=MOMENTS), 15)
        assert "opening" not in sections(result)
        assert result.plan.planned_duration == pytest.approx(15, rel=0.05)


class TestChronologicalStructure:
    def test_the_order_is_purely_chronological(self):
        result = plan(vacation(200, moments=MOMENTS), 300, structure="chronological")
        assert result.report["structure"] == "chronological"
        assert {e.section for e in result.plan.elements} == {"chronological"}
        assert_chronological(result.plan.elements)
        assert result.plan.planned_duration == pytest.approx(300, rel=0.05)


class TestMomentTypes:
    def test_no_more_than_three_clips_of_the_same_type_in_a_row(self):
        # Mostly scenery, but enough of the others to break every run.
        moments = ["scenery", "scenery", "scenery", "scenery", "social", "scenery", "scenery", "action"]
        result = plan(vacation(250, moments=moments), 300)
        assert_no_long_runs(result)
        assert not any(section["moment_runs_relaxed"] for section in result.report["sections"])

    @pytest.mark.parametrize("target", [30, 90, 300])
    def test_the_rule_holds_for_every_target(self, target):
        assert_no_long_runs(plan(vacation(200, moments=MOMENTS), target))

    def test_with_a_single_type_the_duration_wins_and_the_report_says_so(self):
        result = plan(vacation(120, moments=["scenery"]), 120)
        assert result.plan.planned_duration == pytest.approx(120, rel=0.05)
        assert any("not enough variety" in warning for warning in result.report["warnings"])

    def test_transitions_only_link_days_weak_ones_are_dropped_and_their_time_is_capped(self):
        items = vacation(200, moments=["transition", "social", "action", "detail", "scenery"])
        result = plan(items, 300)
        elements = result.plan.elements
        value = {item.id: item.narrative_value for item in items}
        transitions = [n for n, e in enumerate(elements) if e.moment_type == "transition"]
        for n in transitions:
            element = elements[n]
            assert element.section in BODY
            assert value[element.source] >= CONFIG.transition_min_narrative_value
            day = element.captured_at[:10]
            before = elements[n - 1].captured_at[:10] if n else None
            after = elements[n + 1].captured_at[:10] if n + 1 < len(elements) else None
            assert before != day or after != day
        total = sum(elements[n].duration for n in transitions)
        assert total <= CONFIG.transition_time_share * 300 + 0.5

    def test_details_are_short_and_scenery_slightly_longer(self):
        result = plan(vacation(200, moments=MOMENTS), 300)
        body = elements_of(result, "beginning", "development", "finale")
        details = [e.duration for e in body if e.moment_type == "detail"]
        scenery = [e.duration for e in body if e.moment_type == "scenery"]
        assert details and max(details) <= CONFIG.detail_max_seconds + 0.05
        assert scenery and min(scenery) >= CONFIG.photo_min_seconds + CONFIG.scenery_extra_seconds - 0.05


class TestContinuousShots:
    @pytest.mark.parametrize("target", [180, 300])
    def test_a_video_with_several_good_moments_gives_fewer_longer_shots(self, target):
        items = vacation(60, moments=MOMENTS) + ride()  # long action-camera footage is much of the material
        result = plan(items, target)
        disabled = plan(items, target, config=replace(CONFIG, video_shot_max_seconds=CONFIG.video_max_seconds))
        body = pieces(result, RIDE, *BODY)
        assert len(body) < len(pieces(disabled, RIDE, *BODY))
        assert max(e.duration for e in body) > CONFIG.video_max_seconds
        assert all(e.duration <= CONFIG.video_shot_max_seconds + 0.05 for e in body)
        assert result.report["extended_shots"] >= 1 and disabled.report["extended_shots"] == 0
        assert result.plan.planned_duration == pytest.approx(disabled.plan.planned_duration, rel=0.05)
        assert_shots_are_continuous(result, items)

    def test_the_moment_that_chose_the_shot_stays_inside_it(self):
        result = plan(vacation(150, moments=MOMENTS) + ride(), 180)
        long = [e for e in pieces(result, RIDE) if e.duration > CONFIG.video_max_seconds]
        assert long
        for element in long:
            assert "continuous shot" in element.reason
            assert any(element.start <= 10.0 * point + 5.0 <= element.end for point in RIDE_POINTS)

    def test_a_video_with_one_good_moment_keeps_a_normal_clip(self):
        result = plan(vacation(150, moments=MOMENTS) + ride(points=(12,), strong=0.85), 180)
        found = pieces(result, RIDE)
        assert len(found) == 1 and found[0].section in BODY
        assert found[0].duration <= CONFIG.video_max_seconds + 0.05

    def test_shots_only_continue_into_free_adjacent_segments_of_the_same_file(self):
        segments = ride()
        missing = segments[3]  # below the threshold or a duplicate: not in the ranking
        other = ride(name="day2/other.mp4", points=(3, 10))
        items = vacation(150, moments=MOMENTS) + [s for s in segments if s is not missing] + other
        result = plan(items, 300)
        assert_shots_are_continuous(result, items)
        assert not any(e.start < missing.candidate.end and e.end > missing.candidate.start
                       for e in pieces(result, RIDE))

    def test_the_file_cap_and_the_duration_still_hold(self):
        items = vacation(150, moments=MOMENTS) + ride(points=range(0, 30, 2))
        result = plan(items, 180)
        assert sum(e.duration for e in pieces(result, RIDE)) <= CONFIG.max_file_share * 180 + 0.05
        assert result.plan.planned_duration == pytest.approx(180, rel=0.05)
        assert_shots_are_continuous(result, items)

    def test_details_and_short_targets_keep_their_durations(self):
        loose = replace(CONFIG, min_file_cap_seconds=60.0)  # the file cap alone would stop the shots
        details = plan(vacation(150, moments=MOMENTS) + ride(moment="detail"), 180, config=loose)
        assert all(e.duration <= CONFIG.detail_max_seconds + 0.05 for e in pieces(details, RIDE))
        short = plan(vacation(100, moments=MOMENTS) + ride(), 45, config=loose)
        assert pieces(short, RIDE, "body")
        assert all(e.duration <= CONFIG.short_video_max_seconds + 0.05 for e in pieces(short, RIDE, "body"))

    @pytest.mark.parametrize("target", [180, 300])
    def test_a_video_gives_one_piece_per_section_the_opening_too(self, target):
        items = vacation(150, moments=MOMENTS) + ride()
        result = plan(items, target)
        found = pieces(result, RIDE)
        assert pieces(result, RIDE, "opening") and pieces(result, RIDE, *BODY)
        assert len(found) == len({e.section for e in found})
        assert not any(section["video_pieces_relaxed"] for section in result.report["sections"])
        assert_shots_are_continuous(result, items)

    def test_a_second_piece_comes_only_when_the_section_would_run_short(self):
        # Two long clips and a few photos: one piece per clip cannot fill 3 minutes.
        items = vacation(12, moments=MOMENTS) + ride() + ride(name="day2/walk.mp4", when=START + timedelta(days=1, hours=6))
        result = plan(items, 180)
        relaxed = [section for section in result.report["sections"] if section["video_pieces_relaxed"]]
        assert relaxed
        assert len(pieces(result, RIDE, relaxed[0]["name"])) > 1
        assert any("more than one piece" in warning for warning in result.report["warnings"])
        assert_shots_are_continuous(result, items)

    def test_the_closing_is_one_continuous_shot(self):
        late = START + timedelta(days=2, hours=11, minutes=40)  # serene, at the very end of the vacation
        items = vacation(150, moments=MOMENTS) + ride(when=late, moment="scenery", motion_level=0.0)
        result = plan(items, 180)
        closing = pieces(result, RIDE, "closing")
        assert len(closing) == 1 and closing[0].duration > CONFIG.closing_video_max_seconds
        assert "continuous shot" in closing[0].reason
        assert all(e.duration <= CONFIG.opening_clip_max_seconds + 0.05 for e in pieces(result, RIDE, "opening"))
        assert result.plan.planned_duration == pytest.approx(180, rel=0.05)
        assert_shots_are_continuous(result, items)

    @pytest.mark.parametrize("target", [45, 180])
    def test_the_video_of_the_closing_stays_out_of_the_section_before_it(self, target):
        # Without the rule the same video would end the Finale (or the body) and start again in the Closing.
        late = START + timedelta(days=2, hours=11, minutes=40)
        items = vacation(150, moments=MOMENTS) + ride(when=late, moment="scenery", motion_level=0.0)
        result = plan(items, target)
        before_closing = list(sections(result))[-2]
        assert pieces(result, RIDE, "closing")
        assert not pieces(result, RIDE, before_closing)
        assert not any(section["video_pieces_relaxed"] for section in result.report["sections"])
        assert result.plan.planned_duration == pytest.approx(target, rel=0.05)


class TestThePlan:
    def test_the_plan_is_valid_and_carries_what_the_planner_knows(self):
        result = plan(vacation(100, moments=MOMENTS, videos_every=5), 90)
        data = result.plan.to_dict()
        reloaded = plan_from_dict(data)
        assert len(reloaded.elements) == len(result.plan.elements)
        element = data["elements"][1]
        assert {"score", "section", "reason", "captured_at", "date_source", "timestamp"} <= element.keys()
        assert set(element["score"]) == {"final", "technical_score", "narrative_score", "components"}

    def test_video_clips_are_cut_inside_their_segment(self):
        result = plan(vacation(100, moments=MOMENTS, videos_every=3), 90)
        videos = [e for e in result.plan.elements if e.kind == "video"]
        assert videos
        for element in videos:
            assert 0.0 <= element.start < element.end <= 8.0
            assert element.duration == pytest.approx(element.end - element.start)

    def test_one_file_cannot_monopolize_the_video(self):
        long_clip = []
        for n in range(40):  # 40 segments of 8 s of the same file, all excellent
            item = ranked("big.mp4", START + timedelta(minutes=n), kind="video", narrative=0.95,
                          moment=MOMENTS[n % 3 + 1], value=5)
            segment = replace(item.candidate, id=f"big.mp4#{8.0 * n:.3f}-{8.0 * n + 8:.3f}", start=8.0 * n,
                              end=8.0 * n + 8)
            long_clip.append(replace(item, candidate=segment))
        result = plan(vacation(150, moments=MOMENTS) + long_clip, 300)
        from_big = sum(e.duration for e in result.plan.elements if e.source == "big.mp4")
        assert from_big <= CONFIG.max_file_share * 300 + 8

    def test_dissolves_and_cuts(self):
        result = plan(vacation(200, moments=MOMENTS), 300)
        elements = result.plan.elements
        assert elements[0].transition_in.type == "cut"
        assert all(e.transition_in.type == "cut" for e in elements[1:] if e.section == "opening")
        first_body = next(e for e in elements if e.section == "beginning")
        assert first_body.transition_in.type == "dissolve"
