from datetime import timedelta

import pytest
from selection_fakes import START, candidate, metadata, vision

from vacation_memories.duplicate_detector import HashBackend, find_duplicates
from vacation_memories.edit_plan import OutputSpec
from vacation_memories.edit_planner import plan_edit
from vacation_memories.scene_ranker import BELOW_THRESHOLD, MOSTLY_BLACK, rank_candidates, technical_filter
from vacation_memories.selection_config import SelectionConfig
from vacation_memories.transitions import TransitionRules

CONFIG = SelectionConfig()
BACKEND = HashBackend()


def rank(candidates, ai=None, *, ai_used=None):
    passed, discarded = technical_filter(candidates, CONFIG)
    duplicates = find_duplicates([c.duplicate_item() for c in passed], BACKEND)
    survivors = [c for c in passed if c.id in duplicates.representatives()]
    info = {c.rel_path: metadata(START + timedelta(minutes=10 * n), c.kind) for n, c in enumerate(candidates)}
    ai = ai or {}
    ranking = rank_candidates(survivors, info, duplicates, ai, CONFIG, ai_used=bool(ai) if ai_used is None else ai_used)
    return ranking, discarded


def by_id(ranking):
    return {item.id: item for item in ranking.ranked}


class TestTechnicalThreshold:
    def test_a_candidate_below_the_threshold_is_always_discarded(self):
        blurred = candidate("blurred.jpg", technical=CONFIG.technical_threshold - 0.01, seed=1)
        good = candidate("good.jpg", technical=0.9, seed=2)
        # Even with the highest narrative value the AI can give, the blurred shot does not come back.
        ranking, discarded = rank([blurred, good], {"blurred.jpg": vision("action", 5), "good.jpg": vision("scenery", 1)})
        assert [item.id for item in ranking.ranked] == ["good.jpg"]
        assert [(d.id, d.reason) for d in discarded] == [("blurred.jpg", BELOW_THRESHOLD)]

    def test_a_mostly_black_segment_is_discarded(self):
        dark = candidate("night.mp4", technical=0.7, kind="video", start=0.0, end=5.0, motion=0.01, black=0.8)
        _, discarded = technical_filter([dark], CONFIG)
        assert discarded[0].reason == MOSTLY_BLACK

    def test_the_threshold_comes_from_the_configuration(self):
        shot = candidate("a.jpg", technical=0.3)
        assert technical_filter([shot], SelectionConfig(technical_threshold=0.2))[0] == [shot]
        assert technical_filter([shot], SelectionConfig(technical_threshold=0.31))[0] == []

    def test_a_discarded_candidate_never_reaches_the_plan(self):
        blurred = [candidate(f"blurred{n}.jpg", technical=0.1, seed=n) for n in range(10)]
        good = [candidate(f"good{n}.jpg", technical=0.6, seed=100 + n) for n in range(10)]
        ai = {c.id: vision("action", 5) for c in blurred} | {c.id: vision("scenery", 2) for c in good}
        ranking, _ = rank(blurred + good, ai)
        result = plan_edit(ranking, target=60, output=OutputSpec(640, 360), config=CONFIG, rules=TransitionRules())
        assert result.plan.elements and all(e.source.startswith("good") for e in result.plan.elements)


class TestNarrativePrevails:
    def test_a_banal_perfect_shot_does_not_beat_a_strong_moment_above_the_threshold(self):
        banal = candidate("banal.jpg", technical=1.0, seed=1)
        moment = candidate("moment.jpg", technical=CONFIG.technical_threshold + 0.01, seed=2)
        ranking, _ = rank([banal, moment], {"banal.jpg": vision("other", 1), "moment.jpg": vision("social", 5)})
        found = by_id(ranking)
        assert found["moment.jpg"].final_score > found["banal.jpg"].final_score
        assert found["moment.jpg"].narrative_score > found["banal.jpg"].narrative_score
        assert found["banal.jpg"].technical_score > found["moment.jpg"].technical_score

    def test_the_two_dimensions_are_kept_apart(self):
        ranking, _ = rank([candidate("a.jpg", technical=0.77, seed=1)], {"a.jpg": vision("scenery", 4)})
        (item,) = ranking.ranked
        assert item.technical_score == pytest.approx(0.77)
        assert 0 <= item.narrative_score <= 1
        assert item.final_score == pytest.approx(0.65 * item.narrative_score + 0.35 * 0.77)
        assert item.components["ai_value"] == pytest.approx(0.75)

    def test_a_twin_is_less_unique_than_a_one_off(self):
        twin_a = candidate("twin_a.jpg", seed=7)
        twin_b = candidate("twin_b.jpg", seed=7)
        alone = candidate("alone.jpg", seed=8)
        threshold_free = SelectionConfig()
        passed, _ = technical_filter([twin_a, twin_b, alone], threshold_free)
        duplicates = find_duplicates([c.duplicate_item() for c in passed], BACKEND, threshold=1.01)  # no grouping
        info = {c.rel_path: metadata(START + timedelta(hours=n), c.kind) for n, c in enumerate(passed)}
        ranking = rank_candidates(passed, info, duplicates, {}, threshold_free, ai_used=False)
        found = by_id(ranking)
        assert found["twin_a.jpg"].components["uniqueness"] == 0
        assert found["alone.jpg"].components["uniqueness"] > 0.5

    def test_a_dynamic_clip_scores_more_motion_than_a_still_one(self):
        still = candidate("still.mp4", kind="video", start=0.0, end=5.0, motion=0.0, seed=1)
        moving = candidate("moving.mp4", kind="video", start=0.0, end=5.0, motion=0.2, seed=2)
        found = {item.candidate.rel_path.name: item for item in rank([still, moving])[0].ranked}
        assert found["moving.mp4"].components["motion"] == 1.0 and found["still.mp4"].components["motion"] == 0.0


class TestWithoutAi:
    def test_the_selection_works_and_the_report_says_so(self):
        shots = [candidate(f"p{n}.jpg", technical=0.5 + n / 50, seed=n) for n in range(12)]
        ranking, _ = rank(shots, ai_used=False)
        assert not ranking.ai_used
        assert all(set(item.components) == {"uniqueness", "motion", "temporal"} for item in ranking.ranked)
        assert any("without AI" in note for note in ranking.notes)
        result = plan_edit(ranking, target=30, output=OutputSpec(640, 360), config=CONFIG, rules=TransitionRules())
        assert len(result.plan.elements) >= 5
        assert all(element.moment_type is None for element in result.plan.elements)

    def test_partial_coverage_is_reported(self):
        shots = [candidate(f"p{n}.jpg", seed=n) for n in range(4)]
        ranking, _ = rank(shots, {"p0.jpg": vision("scenery", 4)})
        assert ranking.ai_used and any("partial AI coverage" in note for note in ranking.notes)
        found = by_id(ranking)
        assert "ai_value" in found["p0.jpg"].components and "ai_value" not in found["p1.jpg"].components

    def test_a_field_the_model_left_empty_is_simply_missing(self):
        ranking, _ = rank([candidate("a.jpg", seed=1)], {"a.jpg": vision(None, None, None)})
        assert set(ranking.ranked[0].components) == {"uniqueness", "motion", "temporal"}
