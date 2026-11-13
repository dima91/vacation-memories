"""The evaluation vacation: what its ground truth says must be what the analysis finds in the files."""

import json
from collections import Counter
from datetime import datetime

import pytest

from vacation_memories.analysis.duplicate_detector import HashBackend, find_duplicates
from vacation_memories.analysis.media_scanner import scan_media
from vacation_memories.analysis.metadata_extractor import extract_metadata
from vacation_memories.analysis.technical_analysis import analyze_media
from vacation_memories.evaluate import synthetic_media as sm
from vacation_memories.evaluate.ground_truth import (
    GROUND_TRUTH_NAME,
    NEAR_DUPLICATE,
    SIMILAR_SCENE,
    GroundTruthError,
    load_ground_truth,
)
from vacation_memories.planning.scene_ranker import technical_filter
from vacation_memories.planning.selection_config import SelectionConfig

CONFIG = SelectionConfig()


@pytest.fixture(scope="module")
def vacation(tmp_path_factory):
    root = tmp_path_factory.mktemp("evaluation-vacation")
    return root, sm.populate_evaluation_vacation(root, days=2, places_per_day=2)


@pytest.fixture(scope="module")
def candidates(vacation):
    root, _ = vacation
    return analyze_media(scan_media(root), HashBackend())


def scene_of(truth, candidate):
    """The truth of the scene a candidate covers (a clip segment lies inside one scene)."""
    item = truth.file(candidate.rel_path.as_posix())
    if candidate.start is None:
        return item.scenes[0]
    middle = (candidate.start + candidate.end) / 2
    return next(scene for scene in item.scenes if scene.start <= middle < scene.end)


def test_the_ground_truth_lists_every_file_and_reads_back(vacation):
    root, truth = vacation
    on_disk = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.suffix in (".jpg", ".mp4"))
    assert sorted(item.path for item in truth.files) == on_disk
    assert load_ground_truth(root) == truth
    # Per place: three framings of two scenes, a burst of three and a clip; per day a unique and a blurred
    # photo; a black photo on the first day.
    assert Counter(item.kind for item in truth.files) == {"photo": 4 * 9 + 2 * 2 + 1, "video": 4}
    assert Counter(s.important_reason for _, s in truth.scenes() if s.important) == {"unique": 2, "motion": 2}
    assert Counter(s.defect for _, s in truth.scenes() if s.defect) == {"blurred": 2, "black": 2}
    assert truth.places == ("p1", "p2", "p3", "p4")
    assert {item.place for item in truth.files} == set(truth.places)


def test_capture_dates_and_days_are_the_ones_in_the_metadata(vacation):
    root, truth = vacation
    for media in scan_media(root):
        item = truth.file(media.rel_path.as_posix())
        captured = datetime.fromisoformat(item.captured_at)
        assert extract_metadata(media, assume_tz="UTC").capture_datetime == captured
        assert (captured.date() - sm.VACATION_START.date()).days + 1 == item.day
        assert media.rel_path.parts[0].endswith(f"_day{item.day}")


def test_the_hash_groups_every_burst_and_no_reframing(vacation, candidates):
    _, truth = vacation
    photos = [candidate for candidate in candidates if candidate.kind == "photo"]
    found = find_duplicates([candidate.duplicate_item() for candidate in photos], HashBackend())
    group_of = {member.id: number for number, group in enumerate(found.groups) for member in group.members}
    by_truth: dict = {}
    for candidate in photos:
        scene = scene_of(truth, candidate)
        by_truth.setdefault((scene.group_kind, scene.group or candidate.id), []).append(group_of[candidate.id])
    for (kind, _), groups in by_truth.items():
        if kind == NEAR_DUPLICATE:
            assert len(groups) == 3 and len(set(groups)) == 1
        elif kind == SIMILAR_SCENE:
            assert len(groups) == 3 and len(set(groups)) == 3
    assert len(found.duplicate_groups) == sum(1 for kind, _ in by_truth if kind == NEAR_DUPLICATE)


def test_defects_and_only_defects_fall_below_the_technical_threshold(vacation, candidates):
    _, truth = vacation
    _, discarded = technical_filter(candidates, CONFIG)
    by_id = {candidate.id: candidate for candidate in candidates}
    assert {item.id for item in discarded} == {c.id for c in candidates if scene_of(truth, c).defect}
    assert {scene_of(truth, by_id[item.id]).defect for item in discarded} == {"blurred", "black"}


def test_clip_segments_follow_the_scenes_and_fast_pans_are_the_dynamic_ones(vacation, candidates):
    _, truth = vacation
    videos = [candidate for candidate in candidates if candidate.kind == "video"]
    levels = {"motion": [], "calm": []}
    for candidate in videos:
        scene = scene_of(truth, candidate)
        assert candidate.start == pytest.approx(scene.start, abs=0.2)
        assert candidate.end == pytest.approx(scene.end, abs=0.2)
        if not scene.defect:
            levels["motion" if scene.important_reason == "motion" else "calm"].append(
                min(1.0, (candidate.motion or 0.0) / CONFIG.full_motion))
    assert len(videos) == sum(len(item.scenes) for item in truth.files if item.kind == "video")
    assert min(levels["motion"]) >= 0.5 and max(levels["calm"]) <= 0.3, levels


def test_the_same_seed_gives_the_same_vacation(tmp_path):
    first = sm.populate_evaluation_vacation(tmp_path / "a", days=1, places_per_day=1, seed=3)
    second = sm.populate_evaluation_vacation(tmp_path / "b", days=1, places_per_day=1, seed=3)
    assert first == second
    for item in first.files:
        if item.kind == "photo":
            assert (tmp_path / "a" / item.path).read_bytes() == (tmp_path / "b" / item.path).read_bytes()


def test_a_ground_truth_of_another_version_or_inconsistent_is_refused(vacation, tmp_path):
    root, truth = vacation
    with pytest.raises(GroundTruthError, match="not found"):
        load_ground_truth(tmp_path)
    data = truth.to_dict()
    for broken in ({**data, "version": 99},
                   {**data, "files": [{**data["files"][0], "day": 7}]},
                   {**data, "files": [data["files"][0], data["files"][0]]},
                   {**data, "files": [{**data["files"][0], "scenes": [{"scene_id": 1, "start": None, "end": None,
                                                                         "group": "g"}]}]}):
        (tmp_path / GROUND_TRUTH_NAME).write_text(json.dumps(broken))
        with pytest.raises(GroundTruthError):
            load_ground_truth(tmp_path)


def test_the_command_line_writes_the_evaluation_vacation(tmp_path, capsys):
    assert sm.main([str(tmp_path), "--evaluation", "--days", "1", "--places-per-day", "1"]) == 0
    assert (tmp_path / GROUND_TRUTH_NAME).exists()
    assert "wrote" in capsys.readouterr().out
