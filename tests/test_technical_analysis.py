import logging

import numpy as np
import pytest
from image_fakes import natural_image, recompress, scale_brightness
from PIL import ImageFilter

from vacation_memories import synthetic_media as sm
from vacation_memories.duplicate_detector import HashBackend, find_duplicates
from vacation_memories.media_scanner import scan_media
from vacation_memories.technical_analysis import analyze_media

BACKEND = HashBackend()


def analyze(folder, **kwargs):
    return analyze_media(scan_media(folder), BACKEND, **kwargs)


def by_id(candidates):
    return {c.id: c for c in candidates}


@pytest.fixture(scope="module")
def vacation(tmp_path_factory):
    root = tmp_path_factory.mktemp("vacation")
    base = natural_image(1)
    base.save(root / "a.jpg", quality=95)
    recompress(scale_brightness(base, 1.15), 35).save(root / "a_copy.jpg", quality=35)
    base.filter(ImageFilter.GaussianBlur(3)).save(root / "a_blurred.jpg", quality=95)
    natural_image(2).save(root / "b.jpg", quality=95)
    natural_image(3).point(lambda v: v // 40).save(root / "black.jpg")
    sm.make_video(root / "first.mp4", [(1, 2.0), (2, 2.0), (3, 2.0)], 320, 240, fps=10)
    sm.make_video(root / "repeat.mp4", [(1, 2.0), (5, 2.0)], 320, 240, fps=10)
    (root / "corrupt.jpg").write_bytes(b"\x00" * 500)
    (root / "corrupt.mp4").write_bytes(np.random.RandomState(1).bytes(3000))
    return root, by_id(analyze(root))


def test_photos_and_clip_segments_become_candidates(vacation):
    _, found = vacation
    photos = {i for i, c in found.items() if c.kind == "photo"}
    assert photos == {"a.jpg", "a_copy.jpg", "a_blurred.jpg", "b.jpg", "black.jpg"}
    first = sorted((c for c in found.values() if c.rel_path.name == "first.mp4"), key=lambda c: c.start)
    assert [(c.start, round(c.end, 1)) for c in first] == pytest.approx([(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)], abs=0.15)
    assert first[0].id.startswith("first.mp4#0.000-2.")
    assert all(c.kind == "video" and c.motion is not None and c.motion > 0 for c in first)
    assert all(c.start is None and c.end is None and c.motion is None for c in map(found.get, photos))


def test_unreadable_files_are_skipped_and_logged(tmp_path, caplog):
    natural_image(1).save(tmp_path / "ok.jpg")
    (tmp_path / "bad.jpg").write_bytes(b"junk" * 50)
    (tmp_path / "bad.mp4").write_bytes(b"junk" * 50)
    with caplog.at_level(logging.WARNING):
        found = analyze(tmp_path)
    assert [c.id for c in found] == ["ok.jpg"]
    assert "skipping bad.jpg" in caplog.text and "skipping bad.mp4" in caplog.text


def test_technical_scores_rank_blurred_and_black_below_good_photos(vacation):
    _, found = vacation
    assert found["a.jpg"].technical_score > found["a_blurred.jpg"].technical_score
    assert found["b.jpg"].technical_score > found["a_blurred.jpg"].technical_score
    assert found["black.jpg"].technical_score == 0.0 and found["black.jpg"].quality.black_fraction == 1.0
    assert all(0.0 <= c.technical_score <= 1.0 for c in found.values())


def test_a_clip_with_clean_scenes_scores_high_on_every_segment(vacation):
    _, found = vacation
    clips = [c for c in found.values() if c.kind == "video"]
    assert clips and all(c.technical_score > 0.8 for c in clips)


def test_duplicates_across_photos_and_clips_are_grouped_with_the_best_representative(vacation):
    _, found = vacation
    result = find_duplicates([c.duplicate_item() for c in found.values()], BACKEND)
    groups = [sorted(m.id for m in g.members) for g in result.duplicate_groups]

    photo_group = next(g for g in result.duplicate_groups if "a.jpg" in {m.id for m in g.members})
    assert {m.id for m in photo_group.members} == {"a.jpg", "a_copy.jpg", "a_blurred.jpg"}
    assert photo_group.representative in {"a.jpg", "a_copy.jpg"}
    assert found[photo_group.representative].technical_score == max(
        found[m.id].technical_score for m in photo_group.members)

    scene_one = [g for g in groups if any(i.startswith("first.mp4#0.000") for i in g)]
    assert len(scene_one) == 1 and any(i.startswith("repeat.mp4#0.000") for i in scene_one[0])
    flat = {i for g in groups for i in g}
    assert "b.jpg" not in flat and "black.jpg" not in flat
    assert not any(i.startswith(("first.mp4#2.", "first.mp4#4.", "repeat.mp4#2.")) for i in flat)


def test_every_scene_gets_a_sample_even_when_shorter_than_a_second(tmp_path):
    sm.make_video(tmp_path / "quick.mp4", [(1, 0.8), (2, 0.8), (3, 0.8)], 160, 120, fps=10)
    found = analyze(tmp_path)
    assert len(found) == 3 and all(c.quality.frames >= 1 for c in found)
    assert [round(c.start, 1) for c in found] == [0.0, 0.8, 1.6]


def test_long_continuous_clip_is_cut_in_windows_with_few_hash_frames(tmp_path):
    sm.make_video(tmp_path / "long.mp4", [(4, 25.0)], 160, 120, fps=10)
    found = analyze(tmp_path, sample_interval=1.0)
    assert len(found) == 3
    assert all(c.signatures.shape[0] == 3 for c in found)
    assert all(c.quality.frames >= 8 for c in found)


def test_full_reference_dataset_is_analyzed_without_surprises(tmp_path):
    sm.populate(tmp_path, seed=0)
    found = analyze(tmp_path)
    assert len({c.id for c in found}) == len(found)
    assert {c.rel_path.name for c in found if c.kind == "photo"} == {
        "IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.png", "IMG_0004.heic"}
    assert {c.rel_path.name for c in found if c.kind == "video"} == {
        "VID_0001.mp4", "VID_0002.mov", "VID_0003.mkv", "VID_0004.mp4"}
    assert "broken.jpg" not in {c.rel_path.name for c in found}
    assert all(c.signatures.ndim == 2 and c.signatures.shape[1] == 8 for c in found)


def test_cached_candidates_are_the_same_and_the_media_are_not_decoded_again(tmp_path, monkeypatch):
    from vacation_memories import technical_analysis
    from vacation_memories.cache_manager import CacheManager

    natural_image(1).save(tmp_path / "a.jpg", quality=95)
    sm.make_video(tmp_path / "clip.mp4", [(1, 2.0), (2, 2.0)], 320, 240, fps=10)
    files = scan_media(tmp_path)
    with CacheManager(tmp_path / "work") as cache:
        first = analyze_media(files, BACKEND, cache=cache)
        monkeypatch.setattr(technical_analysis, "scan_video", lambda *a, **k: pytest.fail("decoded again"))
        monkeypatch.setattr(technical_analysis, "photo_analysis_frame", lambda *a, **k: pytest.fail("decoded again"))
        second = analyze_media(files, BACKEND, cache=cache)
    assert [c.id for c in second] == [c.id for c in first]
    for a, b in zip(first, second):
        assert (a.start, a.end, a.motion, a.quality) == (b.start, b.end, b.motion, b.quality)
        assert np.array_equal(a.signatures, b.signatures)


def test_the_analysis_can_be_stopped_between_and_inside_files(tmp_path):
    sm.make_video(tmp_path / "clip.mp4", [(1, 12.0)], 160, 120, fps=10)
    calls = []

    def check():
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        analyze_media(scan_media(tmp_path), BACKEND, check=check)
    assert len(calls) == 2  # once before the file, then inside the decode
