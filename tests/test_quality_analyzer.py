import cv2
import numpy as np
import pytest
from image_fakes import gray_of, natural_image
from PIL import ImageFilter

from vacation_memories import synthetic_media as sm
from vacation_memories.frame_sampler import to_analysis_gray
from vacation_memories.quality_analyzer import SegmentQuality, measure_frame


def quality_of(image):
    return measure_frame(gray_of(image))


def test_blur_lowers_sharpness_and_technical_score_monotonically():
    base = natural_image(1)
    scores = [quality_of(base.filter(ImageFilter.GaussianBlur(sigma))) for sigma in (0.01, 1, 2, 4)]
    sharpness = [q.sharpness for q in scores]
    assert sharpness == sorted(sharpness, reverse=True) and sharpness[0] > 0.3 > sharpness[-1]
    totals = [q.technical_score for q in scores]
    assert totals == sorted(totals, reverse=True)


def test_blurred_scene_scores_far_below_the_sharp_one():
    sharp = quality_of(sm.render_scene_frame(3, 640, 480))
    blurred = quality_of(sm.render_scene_frame(3, 640, 480, blur=4))
    assert sharp.technical_score > 0.9
    assert blurred.technical_score < 0.5 * sharp.technical_score
    assert blurred.sharpness < 0.1


def test_black_frame_is_flagged_and_scores_zero():
    for level in (0, 8, 15):
        quality = measure_frame(np.full((384, 512), level, np.uint8))
        assert quality.is_black and quality.technical_score == 0.0


def test_overexposed_frame_scores_zero_without_being_black():
    quality = measure_frame(np.full((384, 512), 252, np.uint8))
    assert not quality.is_black and quality.exposure == 0.0 and quality.technical_score == 0.0


def test_night_scene_with_lights_is_not_penalized_like_a_dark_blur():
    rng = np.random.RandomState(3)
    night = np.asarray(natural_image(2).convert("L")).astype(float) * 0.1
    for _ in range(80):  # street lights: about 2.5% of the pixels
        y, x = rng.randint(20, 440), rng.randint(20, 600)
        night[y:y + 10, x:x + 10] = 235
    night = measure_frame(to_analysis_gray(night.astype(np.uint8)))
    uniformly_dim = measure_frame(to_analysis_gray((np.asarray(natural_image(2).convert("L")) * 0.15).astype(np.uint8)))
    assert not night.is_black
    assert night.exposure > 0.7
    assert night.technical_score > 2 * uniformly_dim.technical_score


def test_flat_scene_is_not_mistaken_for_a_blurred_one():
    sky = np.full((384, 512), 150, np.uint8) + np.random.RandomState(0).randint(0, 3, (384, 512), dtype=np.uint8)
    quality = measure_frame(sky)
    assert quality.sharpness > 0.5 and quality.exposure == 1.0


def test_sharpness_does_not_depend_on_the_source_resolution():
    small = quality_of(natural_image(5, 480, 360))
    large = quality_of(natural_image(5, 480, 360).resize((1920, 1440), resample=1))
    assert abs(small.sharpness - large.sharpness) < 0.25
    assert gray_of(natural_image(5, 480, 360)).shape == gray_of(natural_image(5, 1920, 1440)).shape == (384, 512)


def test_exposure_penalizes_dark_and_bright_versions_of_the_same_photo():
    base = np.asarray(natural_image(4).convert("L")).astype(float)
    normal = measure_frame(to_analysis_gray(base.astype(np.uint8)))
    dark = measure_frame(to_analysis_gray((base * 0.12).astype(np.uint8)))
    bright = measure_frame(to_analysis_gray(np.clip(base * 0.3 + 215, 0, 255).astype(np.uint8)))
    assert normal.exposure == 1.0 and dark.exposure < 0.3 and bright.exposure < 0.3
    assert normal.technical_score > max(dark.technical_score, bright.technical_score)


def test_rejects_frames_that_are_not_grayscale_uint8():
    with pytest.raises(ValueError):
        measure_frame(np.zeros((10, 10, 3), np.uint8))
    with pytest.raises(ValueError):
        measure_frame(np.zeros((10, 10), np.float32))


def test_segment_quality_averages_frames_and_counts_black_ones():
    good = measure_frame(gray_of(natural_image(1)))
    black = measure_frame(np.zeros((384, 512), np.uint8))
    segment = SegmentQuality.from_frames([good, good, good, black])
    assert segment.frames == 4 and segment.black_fraction == 0.25
    assert segment.technical_score == pytest.approx(0.75 * good.technical_score)
    assert SegmentQuality(**segment.to_dict()) == segment
    with pytest.raises(ValueError):
        SegmentQuality.from_frames([])
