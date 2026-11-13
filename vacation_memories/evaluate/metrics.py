"""How a plan does against the known truth of a synthetic vacation.

An element of the plan includes a scene when it shows enough of it: a photo its only scene, a clip
segment every scene it overlaps for at least `INCLUDED_MIN_SECONDS`, or half of a shorter scene.
`METRICS` lists what is measured and which direction is better; time shares use the durations of
the elements, dissolves left aside.
"""

import statistics
from collections import Counter, defaultdict

from vacation_memories.evaluate.ground_truth import (
    MOTION,
    NEAR_DUPLICATE,
    SIMILAR_SCENE,
    UNIQUE,
    GroundTruth,
    TruthFile,
)
from vacation_memories.planning.edit_plan import PHOTO, VIDEO, EditPlan, PlanElement
from vacation_memories.planning.edit_planner import duration_limits
from vacation_memories.planning.selection_config import SelectionConfig

INCLUDED_MIN_SECONDS = 1.0
INCLUDED_MIN_FRACTION = 0.5
RANGE_TOLERANCE_SECONDS = 0.05
DURATION_TOLERANCE_SECONDS = 1e-3
HIGHER, LOWER = "higher", "lower"

METRICS: dict[str, str | None] = {  # name -> the better direction (None: only descriptive)
    "important_recall": HIGHER,  # important moments included, of all those in the vacation
    "important_recall_motion": HIGHER,
    "important_recall_unique": HIGHER,
    "near_duplicate_repeats": LOWER,  # clips beyond the first of the same near-duplicate group
    "similar_scene_repeats": LOWER,  # the same for groups of the same scene framed differently
    "defects_selected": LOWER,  # clips showing a blurred or black scene
    "days_covered": HIGHER,  # share of the days with at least one clip
    "places_covered": HIGHER,
    "day_balance_gap": LOWER,  # largest gap between a day's share of time and its share of material
    "max_source_share": LOWER,  # largest share of time from a single file
    "budget_max_error": LOWER,  # seconds, the largest gap between a section's budget and its planned time
    "sections_in_range": HIGHER,  # share of the sections inside their allowed range
    "durations_in_limits": HIGHER,  # share of the clips inside the duration limits of their section
    "clips": None,
    "planned_duration": None,
}


def plan_metrics(plan: EditPlan, report: dict, truth: GroundTruth, config: SelectionConfig) -> dict:
    """The metrics of a plan; `report` is the planning report (sections, structure, requested duration)."""
    elements = plan.elements
    shown = [_included(element, truth.file(element.source)) for element in elements]
    scenes = {(item.path, index): scene for item in truth.files for index, scene in enumerate(item.scenes)}
    seen = {key for keys in shown for key in keys}

    important = [key for key, scene in scenes.items() if scene.important]
    metrics: dict = {
        "important_recall": _share(sum(key in seen for key in important), len(important)),
    }
    for reason in (MOTION, UNIQUE):
        keys = [key for key in important if scenes[key].important_reason == reason]
        metrics[f"important_recall_{reason}"] = _share(sum(key in seen for key in keys), len(keys))
    for kind in (NEAR_DUPLICATE, SIMILAR_SCENE):
        members = Counter(scenes[key].group for key in seen if scenes[key].group_kind == kind)
        metrics[f"{kind}_repeats"] = sum(count - 1 for count in members.values())
    metrics["defects_selected"] = sum(1 for keys in shown if any(scenes[key].defect for key in keys))

    time = sum(element.duration for element in elements)
    by_day, by_place, by_source = defaultdict(float), defaultdict(float), defaultdict(float)
    for element in elements:
        item = truth.file(element.source)
        by_day[item.day] += element.duration
        by_place[item.place] += element.duration
        by_source[element.source] += element.duration
    material = Counter(item.day for item, scene in truth.scenes() if not scene.defect)
    metrics["days_covered"] = _share(len(by_day), truth.days)
    metrics["places_covered"] = _share(len(by_place), len(truth.places))
    metrics["day_balance_gap"] = None if not time else max(
        abs(by_day[day] / time - material[day] / sum(material.values())) for day in range(1, truth.days + 1))
    metrics["max_source_share"] = None if not time else max(by_source.values()) / time

    sections = report.get("sections", [])
    metrics["budget_max_error"] = max((abs(s["planned"] - s["budget"]) for s in sections), default=None)
    ranged = [s for s in sections if s.get("allowed_range")]
    metrics["sections_in_range"] = _share(
        sum(1 for s in ranged if s["allowed_range"][0] - RANGE_TOLERANCE_SECONDS <= s["planned"]
            <= s["allowed_range"][1] + RANGE_TOLERANCE_SECONDS), len(ranged))
    short = report["requested_duration"] < config.short_target_seconds
    compressed = report.get("structure") == "compressed"
    metrics["durations_in_limits"] = _share(
        sum(1 for element in elements if _within_limits(element, short, compressed, config)), len(elements))
    metrics["clips"] = len(elements)
    metrics["planned_duration"] = plan.planned_duration or 0.0
    return metrics


def compare(full: dict, randoms: list[dict]) -> dict:
    """Each metric of the full plan next to its distribution over the random plans."""
    comparison = {}
    for name, direction in METRICS.items():
        values = [metrics[name] for metrics in randoms if metrics.get(name) is not None]
        entry = {"full": full.get(name), "direction": direction}
        if values:
            entry.update(random_mean=statistics.fmean(values), random_std=statistics.pstdev(values),
                         random_min=min(values), random_max=max(values))
        if direction and values and full.get(name) is not None:
            better = [value < full[name] if direction == HIGHER else value > full[name] for value in values]
            entry["full_better_share"] = sum(better) / len(values)
            entry["tie_share"] = sum(value == full[name] for value in values) / len(values)
        comparison[name] = entry
    return comparison


def _included(element: PlanElement, item: TruthFile) -> list[tuple[str, int]]:
    if element.kind == PHOTO:
        return [(item.path, 0)]
    keys = []
    for index, scene in enumerate(item.scenes):
        overlap = min(element.end, scene.end) - max(element.start, scene.start)
        if overlap >= min(INCLUDED_MIN_SECONDS, INCLUDED_MIN_FRACTION * (scene.end - scene.start)):
            keys.append((item.path, index))
    return keys


def _within_limits(element: PlanElement, short: bool, compressed: bool, config: SelectionConfig) -> bool:
    low, high = duration_limits(element.kind, element.section, element.moment_type, short=short,
                                compressed=compressed, config=config)
    if element.kind == VIDEO and element.section not in ("opening", "closing") and not short:
        high = max(high, config.video_shot_max_seconds)  # a continuous shot
    return low - DURATION_TOLERANCE_SECONDS <= element.duration <= high + DURATION_TOLERANCE_SECONDS


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None
