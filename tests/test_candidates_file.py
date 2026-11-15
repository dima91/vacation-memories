"""`candidates.json`: what the analysis leaves for the editor's catalog. Synthetic media only."""

import json
from collections import Counter

import pytest

from vacation_memories.analysis.runner import (
    CANDIDATES_NAME,
    AnalysisRequest,
    candidates_document,
    run_analysis,
    write_candidates,
)
from vacation_memories.core.config import resolve_settings
from vacation_memories.core.process import ShutdownController
from vacation_memories.core.runtime_profile import DetectionEnv
from vacation_memories.evaluate import synthetic_media as sm
from vacation_memories.rendering.renderer import Progress


def analyze(media, work):
    settings = resolve_settings({"workdir": work, "input": media, "no_ai": True}, {})
    request = AnalysisRequest(input=media, workdir=work, duration=60, assume_tz=settings.assume_tz, no_ai=True,
                              model=settings.model, ai_resolution=settings.ai_resolution, ai_frames=None,
                              ai_time_budget=None)
    with ShutdownController() as controller:
        return run_analysis(request, settings, controller=controller, progress=Progress(None), env=DetectionEnv())


@pytest.fixture(scope="module")
def analyzed(tmp_path_factory):
    """The evaluation vacation: bursts of near-identical photos, blurred and black ones, clips."""
    media = tmp_path_factory.mktemp("candidates-media")
    sm.populate_evaluation_vacation(media, days=2, places_per_day=2)
    work = tmp_path_factory.mktemp("candidates-work")
    analysis = analyze(media, work)
    path = write_candidates(analysis, work)
    return media, work, analysis, json.loads(path.read_text())


def test_it_is_written_atomically_in_the_workdir(analyzed):
    _, work, _, document = analyzed
    assert [p.name for p in work.glob("*candidates*")] == [CANDIDATES_NAME]  # no temporary file left behind
    assert document["version"] == 1 and document["generated_at"]


def test_every_media_file_is_listed_with_the_size_and_mtime_it_had(analyzed):
    media, _, _, document = analyzed
    on_disk = {p.relative_to(media).as_posix(): p.stat() for p in media.rglob("*") if p.suffix in (".jpg", ".mp4")}
    listed = {item["path"]: item for item in document["files"]}
    assert set(listed) == set(on_disk)
    for path, info in on_disk.items():
        assert (listed[path]["size"], listed[path]["mtime_ns"]) == (info.st_size, info.st_mtime_ns)
        assert listed[path]["type"] == ("video" if path.endswith(".mp4") else "photo")
        assert listed[path]["readable"] is True
    videos = [item for item in document["files"] if item["type"] == "video"]
    assert videos and all(item["duration"] > 0 for item in videos)
    assert all(item["duration"] is None for item in document["files"] if item["type"] == "photo")


def test_a_file_that_cannot_be_read_is_listed_as_such(tmp_path):
    media, work = tmp_path / "media", tmp_path / "work"
    sm.populate_vacation(media, photos=2, videos=0, days=1)
    (media / "broken.jpg").write_bytes(b"not a picture")
    document = candidates_document(analyze(media, work))
    broken = next(item for item in document["files"] if item["path"] == "broken.jpg")
    assert broken["readable"] is False and broken["size"] == 13
    assert all(candidate["source"] != "broken.jpg" for candidate in document["candidates"])
    assert sum(item["readable"] for item in document["files"]) == 2  # the analysis is complete for the others


def test_every_candidate_of_the_analysis_is_listed_once(analyzed):
    _, _, analysis, document = analyzed
    ids = [candidate["id"] for candidate in document["candidates"]]
    assert ids == [candidate.id for candidate in analysis.candidates]
    assert len(set(ids)) == len(ids)
    by_status = Counter(candidate["status"] for candidate in document["candidates"])
    assert by_status["eligible"] == analysis.counts["eligible"]
    assert by_status["discarded"] == len(analysis.candidates) - analysis.counts["eligible"]
    assert document["counts"] == analysis.counts


def test_a_candidate_has_its_identity_and_its_extent(analyzed):
    _, _, analysis, document = analyzed
    for candidate in document["candidates"]:
        if candidate["type"] == "photo":
            assert candidate["id"] == candidate["source"] and candidate["start"] is None and candidate["end"] is None
        else:
            assert candidate["id"] == f"{candidate['source']}#{candidate['start']:.3f}-{candidate['end']:.3f}"
            assert 0 <= candidate["start"] < candidate["end"]


def test_the_discard_reasons_are_black_blurred_and_duplicate(analyzed):
    _, _, analysis, document = analyzed
    reasons = Counter(candidate["discard"]["reason"] for candidate in document["candidates"] if candidate["discard"])
    assert set(reasons) == {"mostly_black", "below_technical_threshold", "duplicate"}
    assert reasons["duplicate"] == analysis.counts["duplicates"]
    assert reasons["mostly_black"] + reasons["below_technical_threshold"] == analysis.counts["below_technical_threshold"]
    for candidate in document["candidates"]:
        assert (candidate["discard"] is None) == (candidate["status"] == "eligible")
        if candidate["discard"]:
            assert candidate["discard"]["detail"]


def test_scores_are_the_ones_the_plan_uses(analyzed):
    _, _, analysis, document = analyzed
    ranked = {item.id: item for item in analysis.ranking.ranked}
    for candidate in document["candidates"]:
        score = candidate["score"]
        assert 0 <= score["technical_score"] <= 1
        if candidate["status"] == "eligible":
            item = ranked[candidate["id"]]
            assert score["final"] == pytest.approx(item.final_score, abs=1e-4)
            assert score["narrative_score"] == pytest.approx(item.narrative_score, abs=1e-4)
            assert score["components"] == item.components and "uniqueness" in score["components"]
        else:  # never ranked: only the technical score is known
            assert score["final"] is None and score["narrative_score"] is None and score["components"] is None


def test_the_ai_fields_are_empty_without_ai(analyzed):
    _, _, _, document = analyzed
    assert document["ai"] == {"used": False, "coverage": None}
    for candidate in document["candidates"]:
        assert (candidate["moment_type"], candidate["category"], candidate["narrative_value"],
                candidate["description"]) == (None, None, None, None)


def test_the_day_and_the_capture_time_of_every_candidate(analyzed):
    _, _, _, document = analyzed
    days = {candidate["day"] for candidate in document["candidates"]}
    assert len(days) == 2
    for candidate in document["candidates"]:
        assert candidate["captured_at"].startswith(candidate["day"])


def test_near_duplicates_share_a_group_with_their_representative(analyzed):
    _, _, analysis, document = analyzed
    candidates = {candidate["id"]: candidate for candidate in document["candidates"]}
    groups = analysis.duplicates.duplicate_groups
    assert groups
    for group in groups:
        for member in group.members:
            info = candidates[member.id]["group"]
            assert info == {"representative": group.representative, "size": len(group.members)}
        assert candidates[group.representative]["status"] == "eligible"
        assert all(candidates[member.id]["discard"]["reason"] == "duplicate" for member in group.duplicates)
    grouped = {member.id for group in groups for member in group.members}
    assert all((candidate["group"] is None) == (candidate["id"] not in grouped) for candidate in candidates.values())


def test_no_absolute_path_anywhere(analyzed):
    media, work, _, document = analyzed
    text = json.dumps(document)
    assert str(media) not in text and str(work) not in text
    paths = [item["path"] for item in document["files"]] + [c["source"] for c in document["candidates"]]
    assert paths and not any(path.startswith("/") or ".." in path.split("/") for path in paths)


def test_writing_again_replaces_the_file(analyzed):
    _, work, analysis, _ = analyzed
    first = (work / CANDIDATES_NAME).read_text()
    write_candidates(analysis, work)
    assert json.loads((work / CANDIDATES_NAME).read_text())["candidates"] == json.loads(first)["candidates"]
