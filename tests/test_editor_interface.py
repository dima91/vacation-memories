"""What the interface (`frontend/`) must stay in step with on the Python side: the texts of the server's codes."""

import re
from pathlib import Path

from vacation_memories.editor import jobs
from vacation_memories.planning import scene_ranker

ROOT = Path(__file__).resolve().parent.parent
CATALOG = (ROOT / "frontend" / "src" / "i18n" / "it.ts").read_text(encoding="utf-8")
EDITOR = ROOT / "vacation_memories" / "editor"


def catalog_keys() -> set[str]:
    return set(re.findall(r'^\s*"([^"]+)":', CATALOG, flags=re.MULTILINE))


def server_error_codes() -> set[str]:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in EDITOR.glob("*.py"))
    codes = set(re.findall(r'ApiError\(\s*\d+,\s*"(\w+)"', sources))
    codes |= set(re.findall(r'\b(?:bad_request|conflict)\(\s*"(\w+)"', sources))
    codes.add("not_found")  # `not_found(...)` has a fixed code
    return codes


def test_every_error_code_of_the_server_has_a_text_in_the_catalog():
    codes = server_error_codes()
    assert len(codes) >= 20, "the search for the codes found too few"
    missing = sorted(code for code in codes if f"error.{code}" not in catalog_keys())
    assert missing == [], f"add them to frontend/src/i18n/it.ts: {missing}"


def test_every_exit_message_key_of_the_jobs_has_a_text():
    keys = {key for key, _ in jobs._EXIT_MESSAGES.values()} | {"interrupted", "failed"}
    assert sorted(key for key in keys if f"exit.{key}" not in catalog_keys()) == []


def test_every_warning_of_the_plan_document_has_a_text():
    server = (EDITOR / "server.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'"code": "(\w+)"', server)) - {"planner"}  # the planner's own message is shown as it is
    assert {"media_missing", "duration_off"} <= codes
    assert sorted(code for code in codes if f"warning.{code}" not in catalog_keys()) == []


def test_every_phase_of_the_analysis_and_the_rendering_has_a_text():
    phases = set()
    for relative in ("analysis/runner.py", "pipeline.py", "rendering/renderer.py"):
        phases |= set(re.findall(r'progress\.phase\("([^"]+)"', (ROOT / "vacation_memories" / relative).read_text(encoding="utf-8")))
    assert {"scanning", "technical analysis", "planning", "encoding the video"} <= phases
    assert sorted(name for name in phases if f"phase.{name}" not in catalog_keys()) == []


def test_every_discard_reason_of_the_analysis_has_a_text():
    reasons = {scene_ranker.BELOW_THRESHOLD, scene_ranker.MOSTLY_BLACK}
    assert sorted(reason for reason in reasons if f"discard.{reason}" not in catalog_keys()) == []


def test_the_states_of_a_folder_and_of_a_requirement_have_a_text():
    for state in ("not_analyzed", "incomplete", "analyzed", "plan_edited", "rendered"):
        assert f"state.{state}" in catalog_keys()
    for requirement in ("analysis", "plan", "render", "music"):
        assert f"requires.{requirement}" in catalog_keys()
