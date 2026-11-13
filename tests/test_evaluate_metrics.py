"""The metrics of `evaluate synthetic` on hand-made plans and ground truths, where every value is known."""

import pytest

from vacation_memories.evaluate.ground_truth import GroundTruth, TruthFile, TruthScene
from vacation_memories.evaluate.metrics import METRICS, compare, plan_metrics
from vacation_memories.planning.edit_plan import EditPlan, OutputSpec, PlanElement
from vacation_memories.planning.selection_config import SelectionConfig

CONFIG = SelectionConfig()


def photo_file(path, day, place, **scene):
    return TruthFile(path, "photo", day, place, "2023-07-14T09:00:00", (TruthScene(1, None, None, **scene),))


TRUTH = GroundTruth(
    seed=0, params={}, days=2, places=("p1", "p2", "p3"),
    files=(
        photo_file("d1/burst1.jpg", 1, "p1", group="b", group_kind="near_duplicate"),
        photo_file("d1/burst2.jpg", 1, "p1", group="b", group_kind="near_duplicate"),
        photo_file("d1/burst3.jpg", 1, "p1", group="b", group_kind="near_duplicate"),
        photo_file("d1/view1.jpg", 1, "p1", group="s", group_kind="similar_scene"),
        photo_file("d1/view2.jpg", 1, "p1", group="s", group_kind="similar_scene"),
        photo_file("d1/unique.jpg", 1, "p2", important=True, important_reason="unique"),
        photo_file("d2/blurred.jpg", 2, "p3", defect="blurred"),
        TruthFile("d2/clip.mp4", "video", 2, "p3", "2023-07-15T10:00:00", (
            TruthScene(10, 0.0, 4.0),
            TruthScene(11, 4.0, 8.0, important=True, important_reason="motion"),
            TruthScene(12, 8.0, 12.0, defect="black"),
        )),
    ),
)


def photo(source, section="development", duration=4.0):
    return PlanElement(source, "photo", duration, section=section)


def clip(start, end, section="development"):
    return PlanElement("d2/clip.mp4", "video", end - start, start=start, end=end, section=section)


def metrics_of(elements, *, sections=None, structure="narrative", requested=60.0):
    plan = EditPlan(OutputSpec(1280, 720), elements, requested_duration=requested,
                    planned_duration=sum(e.duration for e in elements))
    report = {"sections": sections or [], "structure": structure, "requested_duration": requested}
    return plan_metrics(plan, report, TRUTH, CONFIG)


def test_every_metric_is_reported():
    assert set(metrics_of([photo("d1/unique.jpg")])) == set(METRICS)


def test_important_moments_count_when_the_clip_shows_enough_of_the_scene():
    # 0.9 s of the motion scene (4.0-4.9) is less than the 1 s needed; 1 s (3.5-5.0) is enough.
    too_little = metrics_of([photo("d1/unique.jpg"), clip(1.0, 4.9)])
    assert too_little["important_recall"] == 0.5
    assert too_little["important_recall_unique"] == 1.0 and too_little["important_recall_motion"] == 0.0
    enough = metrics_of([clip(4.0, 5.0)])
    assert enough["important_recall_motion"] == 1.0 and enough["important_recall_unique"] == 0.0


def test_repeats_count_the_clips_beyond_the_first_of_a_group():
    found = metrics_of([photo("d1/burst1.jpg"), photo("d1/burst2.jpg"), photo("d1/burst3.jpg"),
                        photo("d1/view1.jpg"), photo("d1/unique.jpg")])
    assert found["near_duplicate_repeats"] == 2
    assert found["similar_scene_repeats"] == 0
    assert metrics_of([photo("d1/view1.jpg"), photo("d1/view2.jpg")])["similar_scene_repeats"] == 1


def test_defects_count_the_clips_that_show_one():
    found = metrics_of([photo("d2/blurred.jpg"), clip(7.0, 9.5), clip(0.0, 3.0)])
    assert found["defects_selected"] == 2  # the blurred photo and the clip reaching into the black scene


def test_coverage_balance_and_source_share():
    # 8 s on day 1 (two places) and 4 s on day 2; the material is 6 scenes on day 1 and 2 (no defects) on day 2.
    found = metrics_of([photo("d1/view1.jpg"), photo("d1/unique.jpg"), clip(0.0, 4.0)])
    assert found["days_covered"] == 1.0
    assert found["places_covered"] == 1.0
    assert found["day_balance_gap"] == pytest.approx(abs(8 / 12 - 6 / 8))
    assert found["max_source_share"] == pytest.approx(4 / 12)
    one = metrics_of([photo("d1/view1.jpg")])
    assert one["days_covered"] == 0.5 and one["places_covered"] == pytest.approx(1 / 3)
    assert one["max_source_share"] == 1.0


def test_budgets_ranges_and_duration_limits():
    sections = [
        {"name": "opening", "budget": 6.0, "planned": 5.5, "allowed_range": [3.0, 9.0]},
        {"name": "development", "budget": 27.0, "planned": 29.0, "allowed_range": [21.0, 28.0]},
        {"name": "closing", "budget": 4.0, "planned": 4.0, "allowed_range": None},
    ]
    elements = [photo("d1/unique.jpg", "opening", 2.5), photo("d1/view1.jpg", "development", 3.5),
                photo("d1/view2.jpg", "development", 5.0),  # longer than a photo may last
                clip(0.0, 12.0)]  # a continuous shot may reach video_shot_max_seconds
    found = metrics_of(elements, sections=sections, requested=60.0)
    assert found["budget_max_error"] == pytest.approx(2.0)
    assert found["sections_in_range"] == 0.5
    assert found["durations_in_limits"] == 0.75
    assert found["clips"] == 4 and found["planned_duration"] == pytest.approx(23.0)


def test_an_empty_plan_has_no_shares():
    found = metrics_of([])
    assert found["important_recall"] == 0.0
    assert found["day_balance_gap"] is None and found["max_source_share"] is None
    assert found["durations_in_limits"] is None and found["clips"] == 0


def test_compare_puts_the_full_plan_against_the_random_distribution():
    full = {"important_recall": 0.8, "defects_selected": 0, "clips": 20}
    randoms = [{"important_recall": 0.4, "defects_selected": 2, "clips": 18},
               {"important_recall": 0.8, "defects_selected": 0, "clips": 22},
               {"important_recall": 1.0, "defects_selected": 1, "clips": 20}]
    found = compare(full, randoms)
    recall = found["important_recall"]
    assert recall["full"] == 0.8 and recall["direction"] == "higher"
    assert recall["random_mean"] == pytest.approx(2.2 / 3)
    assert (recall["random_min"], recall["random_max"]) == (0.4, 1.0)
    assert recall["full_better_share"] == pytest.approx(1 / 3) and recall["tie_share"] == pytest.approx(1 / 3)
    assert found["defects_selected"]["full_better_share"] == pytest.approx(2 / 3)  # lower is better
    assert "full_better_share" not in found["clips"]
    assert found["day_balance_gap"] == {"full": None, "direction": "lower"}
