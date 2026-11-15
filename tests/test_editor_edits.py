"""What the interface saves after the user's edits is what the server's rules accept.

`tests/fixtures/editor/edited_plans.json` is written by the client tests (`frontend/test/scenarios.ts`): the plan of the
shared fixture after sequences of edits (add, remove, reorder, durations, trims, exclusions). The client keeps the plan
inside the limits the server sent and settles the transitions by itself; the server checks again on every save. Here the
same plans go through the loader and `check_plan`, so a rule the client does not know about fails in this file.
"""

import json
from pathlib import Path

import pytest

from vacation_memories.planning.edit_plan import PHOTO, VIDEO, finalize_timeline, plan_from_dict
from vacation_memories.planning.plan_rules import check_plan, plan_duration, plan_limits
from vacation_memories.planning.transitions import TransitionRules

FIXTURES = Path(__file__).parent / "fixtures" / "editor"
RULES = TransitionRules()


def edited_plans() -> list[dict]:
    return json.loads((FIXTURES / "edited_plans.json").read_text(encoding="utf-8"))


def media_durations() -> dict[str, float]:
    catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
    return {video["source"]: video["duration"] for video in catalog["videos"] if video["duration"]}


@pytest.mark.parametrize("scenario", edited_plans(), ids=lambda scenario: scenario["name"])
def test_the_plan_after_the_edits_respects_the_rules(scenario):
    plan = plan_from_dict(scenario["plan"])
    assert check_plan(plan, RULES, media_durations=media_durations()) == []
    finished, _layout = finalize_timeline(plan, RULES)
    assert [element.id for element in finished.elements] == [element["id"] for element in scenario["plan"]["elements"]]
    assert plan_duration(finished, RULES) == finished.planned_duration
    limits = plan_limits(finished, RULES, media_durations=media_durations())
    assert set(limits.elements) == {element.id for element in finished.elements}, "the server has limits for every clip"


def test_the_scenarios_cover_every_kind_of_edit():
    plans = edited_plans()
    assert len(plans) >= 15
    base = json.loads((FIXTURES / "plan_document.json").read_text(encoding="utf-8"))["plan"]
    base_ids = [element["id"] for element in base["elements"]]
    kinds = set()
    for scenario in plans:
        plan = scenario["plan"]
        ids = [element["id"] for element in plan["elements"]]
        if len(ids) < len(base_ids):
            kinds.add("removal")
        if len(ids) > len(base_ids):
            kinds.add("addition")
        if sorted(ids) == sorted(base_ids) and ids != base_ids:
            kinds.add("reorder")
        if len(plan["excluded"]) != len(base["excluded"]):
            kinds.add("exclusion")
        for element in plan["elements"]:
            if element["type"] == PHOTO and element.get("duration_origin") == "user":
                kinds.add("duration")
            if element["type"] == VIDEO and element["id"] in base_ids:
                before = next(entry for entry in base["elements"] if entry["id"] == element["id"])
                if (before["start"], before["end"]) != (element["start"], element["end"]):
                    kinds.add("trim")
    assert kinds >= {"removal", "addition", "reorder", "exclusion", "duration", "trim"}
