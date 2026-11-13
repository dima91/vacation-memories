"""The random baseline of the evaluation (`plan_random`): the planner's sections and durations, filled by chance."""

from dataclasses import replace

import pytest
from selection_fakes import ranking_of, vacation

from vacation_memories.planning.edit_plan import OutputSpec
from vacation_memories.planning.edit_planner import duration_limits, plan_edit, plan_random
from vacation_memories.planning.selection_config import SelectionConfig
from vacation_memories.planning.transitions import TransitionRules

CONFIG = SelectionConfig()
OUTPUT = OutputSpec(1280, 720)
RULES = TransitionRules()
MOMENTS = ["scenery", "social", "action", "detail", "social", "action", "other"]


def by_chance(items, target, seed):
    return plan_random(ranking_of(items), target=target, output=OUTPUT, config=CONFIG, rules=RULES, seed=seed)


def planned(items, target):
    return plan_edit(ranking_of(items), target=target, output=OUTPUT, config=CONFIG, rules=RULES)


def chosen(result):
    return [(element.source, element.start) for element in result.plan.elements]


@pytest.fixture(scope="module")
def items():
    return vacation(150, moments=MOMENTS, videos_every=6)


def test_the_same_seed_gives_the_same_plan_and_another_seed_a_different_one(items):
    first = by_chance(items, 90, 7)
    assert first.plan.to_dict() == by_chance(items, 90, 7).plan.to_dict()
    assert chosen(first) != chosen(by_chance(items, 90, 8))
    assert first.report["random_seed"] == 7
    assert all(element.reason == "random baseline (seed 7)" for element in first.plan.elements)


@pytest.mark.parametrize("target", [30, 90, 300])
def test_it_keeps_the_structure_and_the_budgets_of_the_planner(items, target):
    chance, full = by_chance(items, target, 1), planned(items, target)
    assert chance.report["structure"] == full.report["structure"]
    assert [s["name"] for s in chance.report["sections"]] == [s["name"] for s in full.report["sections"]]
    for section in chance.report["sections"]:
        if section["allowed_range"]:
            low, high = section["allowed_range"]
            assert low <= section["planned"] <= high, section
        assert section["planned"] == pytest.approx(section["budget"], rel=0.05, abs=1.0), section
    assert chance.plan.planned_duration == pytest.approx(target, rel=0.05)


@pytest.mark.parametrize("seed", range(5))
def test_every_clip_lasts_what_its_section_allows_and_nothing_is_used_twice(items, seed):
    result = by_chance(items, 90, seed)
    for element in result.plan.elements:
        low, high = duration_limits(element.kind, element.section, element.moment_type, short=False,
                                    compressed=False, config=CONFIG)
        assert low - 1e-3 <= element.duration <= high + 1e-3, element
    assert len(set(chosen(result))) == len(result.plan.elements)


def test_each_section_is_in_chronological_order(items):
    elements = by_chance(items, 300, 2).plan.elements
    for name in {element.section for element in elements}:
        times = [element.captured_at for element in elements if element.section == name]
        assert times == sorted(times), name


def test_it_ignores_scores_and_rules():
    """The weakest candidates are taken as readily as the best ones, below the technical threshold too."""
    items = vacation(150, moments=["action"] * 7)  # one moment type: the planner would cap its runs
    weak = {item.id for item in items[::3]}
    items = [replace(item, technical_score=0.05, narrative_score=0.0, final_score=0.0) if item.id in weak else item
             for item in items]
    picked = [element.source for seed in range(10) for element in by_chance(items, 90, seed).plan.elements]
    share = sum(1 for source in picked if source in weak) / len(picked)
    assert 0.2 < share < 0.5  # about a third of the material, by chance
    body = {e.source for e in planned(items, 90).plan.elements if e.section not in ("opening", "closing")}
    assert body and not body & weak  # the planner, by score, never needs them
