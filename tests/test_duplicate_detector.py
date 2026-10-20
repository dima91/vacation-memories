import numpy as np
import pytest
from image_fakes import gray_of, natural_image, recompress, resize_slightly, scale_brightness
from PIL import ImageFilter

from vacation_memories import synthetic_media as sm
from vacation_memories.duplicate_detector import (
    DuplicateItem,
    HashBackend,
    find_duplicates,
)
from vacation_memories.quality_analyzer import measure_frame

BACKEND = HashBackend()


def item(name, image, narrative=None):
    gray = gray_of(image)
    return DuplicateItem(name, BACKEND.signature(gray)[None, :], measure_frame(gray).technical_score, narrative)


def groups_of(result):
    return sorted(sorted(m.id for m in g.members) for g in result.groups)


def test_nearly_identical_variants_are_grouped_together():
    base = natural_image(1)
    variants = {
        "base": base,
        "brighter": scale_brightness(base, 1.2),
        "darker": scale_brightness(base, 0.8),
        "jpeg-q30": recompress(base, 30),
        "smaller": resize_slightly(base, 0.9),
        "much-smaller": resize_slightly(base, 0.75),
        "blurred": base.filter(ImageFilter.GaussianBlur(2)),
    }
    result = find_duplicates([item(k, v) for k, v in variants.items()], BACKEND)
    assert groups_of(result) == [sorted(variants)]


def test_different_images_are_not_grouped():
    items = [item(f"natural-{seed}", natural_image(seed)) for seed in range(1, 31)]
    result = find_duplicates(items, BACKEND)
    assert len(result.groups) == 30 and not result.duplicate_groups


def test_distinct_scenes_are_not_grouped():
    items = [item(f"scene-{n}", sm.render_scene_frame(n, 640, 480, seed=2)) for n in range(6)]
    result = find_duplicates(items, BACKEND)
    assert not result.duplicate_groups


def test_variants_do_not_pull_in_a_different_photo():
    base = natural_image(1)
    items = [item("a", base), item("a-jpeg", recompress(base, 40)), item("b", natural_image(2)),
             item("b-bright", scale_brightness(natural_image(2), 1.15)), item("c", natural_image(3))]
    assert groups_of(find_duplicates(items, BACKEND)) == [["a", "a-jpeg"], ["b", "b-bright"], ["c"]]


def test_representative_is_the_technically_best_of_the_group():
    base = natural_image(1)
    sharp = item("sharp", base)
    blurred = item("blurred", base.filter(ImageFilter.GaussianBlur(2)))
    dark = item("dark", scale_brightness(base, 0.25))
    for order in ([blurred, sharp, dark], [dark, blurred, sharp], [sharp, dark, blurred]):
        (group,) = find_duplicates(order, BACKEND).groups
        assert group.representative == "sharp"
        assert group.members[0].reason is None


def test_ties_on_technical_score_are_broken_by_narrative_score():
    base = natural_image(1)
    plain = item("plain", base, narrative=0.2)
    meaningful = item("meaningful", base, narrative=0.9)
    assert plain.technical_score == meaningful.technical_score
    (group,) = find_duplicates([plain, meaningful], BACKEND).groups
    assert group.representative == "meaningful"
    (group,) = find_duplicates([meaningful, plain], BACKEND).groups
    assert group.representative == "meaningful"


def test_full_ties_keep_the_first_item_so_results_are_deterministic():
    base = natural_image(1)
    (group,) = find_duplicates([item("first", base), item("second", base)], BACKEND).groups
    assert group.representative == "first"


def test_members_record_the_reason_and_the_similarity():
    base = natural_image(1)
    keep = DuplicateItem("keep", item("keep", base).signatures, 0.9)
    copy = DuplicateItem("copy", item("copy", recompress(base, 30)).signatures, 0.5)
    result = find_duplicates([keep, copy], BACKEND)
    (group,) = result.duplicate_groups
    (duplicate,) = group.duplicates
    assert duplicate.id == "copy" and duplicate.similarity >= BACKEND.threshold
    assert "near-duplicate of keep" in duplicate.reason and "hash" in duplicate.reason


def test_similarity_matrix_is_symmetric_with_unit_diagonal():
    items = [item(f"n{seed}", natural_image(seed)) for seed in range(1, 6)]
    matrix = find_duplicates(items, BACKEND).similarity
    assert matrix.shape == (5, 5) and matrix.dtype == np.float32
    assert np.array_equal(matrix, matrix.T)
    assert np.all(np.diag(matrix) == 1.0)
    assert np.all((matrix >= 0) & (matrix <= 1))


def test_threshold_override_changes_the_grouping():
    base = natural_image(1)
    items = [item("a", base), item("b", natural_image(2))]
    assert not find_duplicates(items, BACKEND).duplicate_groups
    assert len(find_duplicates(items, BACKEND, threshold=0.0).duplicate_groups) == 1


def test_hash_distance_limit_is_respected_exactly():
    zero = np.zeros((1, 8), np.uint8)
    flipped = lambda bits: np.packbits(np.array([1] * bits + [0] * (64 - bits), dtype=np.uint8))[None, :]  # noqa: E731
    backend = HashBackend(max_distance=6)
    near = DuplicateItem("near", flipped(6), 0.5)
    far = DuplicateItem("far", flipped(7), 0.5)
    anchor = DuplicateItem("anchor", zero, 0.5)
    result = find_duplicates([anchor, near, far], backend)
    assert result.similarity[0, 1] >= backend.threshold > result.similarity[0, 2]


def test_grouping_chains_through_connected_components():
    def signature(bits_set):
        bits = np.zeros(64, np.uint8)
        bits[:bits_set] = 1
        return np.packbits(bits)[None, :]

    items = [DuplicateItem("a", signature(0), 0.5), DuplicateItem("b", signature(5), 0.6),
             DuplicateItem("c", signature(10), 0.7), DuplicateItem("far", signature(40), 0.9)]
    result = find_duplicates(items, HashBackend(max_distance=6))
    assert groups_of(result) == [["a", "b", "c"], ["far"]]
    (group,) = result.duplicate_groups
    assert group.representative == "c"
    chained = next(m for m in group.duplicates if m.id == "a")
    assert chained.similarity < HashBackend(max_distance=6).threshold  # linked through b, not directly


def test_video_segments_match_on_any_of_their_frames():
    base, other = natural_image(1), natural_image(2)
    clip_a = DuplicateItem("clip-a", np.stack([BACKEND.signature(gray_of(base)), BACKEND.signature(gray_of(natural_image(7)))]), 0.5)
    clip_b = DuplicateItem("clip-b", np.stack([BACKEND.signature(gray_of(other)), BACKEND.signature(gray_of(recompress(base, 35)))]), 0.4)
    clip_c = DuplicateItem("clip-c", np.stack([BACKEND.signature(gray_of(natural_image(8)))]), 0.4)
    assert groups_of(find_duplicates([clip_a, clip_b, clip_c], BACKEND)) == [["clip-a", "clip-b"], ["clip-c"]]


def test_blocks_of_many_items_match_a_direct_computation():
    rng = np.random.RandomState(0)
    items = [DuplicateItem(f"i{n}", rng.randint(0, 256, (rng.randint(1, 4), 8), dtype=np.uint8), 0.5) for n in range(60)]
    matrix = find_duplicates(items, BACKEND).similarity
    for a in (0, 17, 59):
        for b in (3, 30, 58):
            if a != b:
                direct = BACKEND.similarity(items[a].signatures, items[b].signatures).max()
                assert matrix[a, b] == pytest.approx(direct)


def test_dhash_confirmation_needs_both_hashes_to_agree():
    confirming = HashBackend(max_distance=6, confirm_with_dhash=True)
    assert confirming.signature(gray_of(natural_image(1))).shape == (16,)
    base = natural_image(1)
    result = find_duplicates(
        [DuplicateItem("a", confirming.signature(gray_of(base))[None, :], 0.5),
         DuplicateItem("b", confirming.signature(gray_of(recompress(base, 30)))[None, :], 0.4)],
        confirming)
    assert len(result.duplicate_groups) == 1
    left = np.concatenate([np.zeros(8, np.uint8), np.zeros(8, np.uint8)])[None, :]
    right = np.concatenate([np.zeros(8, np.uint8), np.full(8, 0xFF, np.uint8)])[None, :]  # only the dHash differs
    assert confirming.similarity(left, right)[0, 0] < confirming.threshold


class IdenticalOnlyBackend:
    """A replaceable backend: signatures are the exact pixel sums, similarity is equality."""

    name = "fake"
    threshold = 1.0

    def signature(self, gray):
        return np.array([int(gray.sum()) % 251, int(gray.sum()) % 241], dtype=np.int64)

    def similarity(self, left, right):
        return (left[:, None, :] == right[None, :, :]).all(axis=2).astype(np.float32)


def test_the_backend_can_be_replaced_without_touching_the_callers():
    backend = IdenticalOnlyBackend()
    base = natural_image(1)
    items = [
        DuplicateItem("same-1", backend.signature(gray_of(base))[None, :], 0.5),
        DuplicateItem("same-2", backend.signature(gray_of(base))[None, :], 0.7),
        DuplicateItem("other", backend.signature(gray_of(natural_image(2)))[None, :], 0.9),
    ]
    result = find_duplicates(items, backend)
    assert result.backend == "fake" and groups_of(result) == [["other"], ["same-1", "same-2"]]
    assert "fake similarity" in result.duplicate_groups[0].duplicates[0].reason


def test_empty_input_and_invalid_items():
    assert find_duplicates([], BACKEND).groups == []
    one = item("a", natural_image(1))
    with pytest.raises(ValueError):
        find_duplicates([one, one], BACKEND)
    with pytest.raises(ValueError):
        find_duplicates([DuplicateItem("empty", np.zeros((0, 8), np.uint8), 0.1)], BACKEND)


@pytest.mark.parametrize("kwargs", [{"max_distance": 64}, {"max_distance": -1}, {"hash_size": 6}, {"hash_size": 0}])
def test_invalid_backend_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        HashBackend(**kwargs)
