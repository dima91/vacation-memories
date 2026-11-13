"""Cheap technical quality of a frame: sharpness, exposure, contrast, black frames.

Every measure works on the grayscale analysis frame of `frame_sampler` (fixed long side), so the
scores do not depend on the resolution of the source. `technical_score` (0-1) describes only how
well the shot turned out technically; the narrative value is a separate matter (section 6).

Night scenes are not penalized systematically:
- sharpness is the variance of the Laplacian divided by the variance of the luma, so a dark or
  low-contrast scene is judged on its own detail and not on its brightness;
- when the luma barely varies (clear sky, fog) sharpness cannot be measured and moves towards a
  neutral value instead of reading as "blurred";
- exposure looks at the bright end (99th percentile) for under-exposure, so a dark scene with
  visible lights is fine, while a uniformly dark frame is not.

The constants below are tuned on synthetic images only and are meant to be adjusted on real material
by the user.
"""

from dataclasses import asdict, dataclass

import cv2
import numpy as np

SHARPNESS_SCALE = 0.04  # relative Laplacian variance that gives a sharpness of about 0.63
LUMA_VARIANCE_FLOOR = 25.0  # keeps the ratio stable on almost flat frames
CONTRAST_STD_FOR_RELIABLE_SHARPNESS = 10.0
NEUTRAL_SHARPNESS = 0.6
UNDEREXPOSED_P99 = (25.0, 110.0)  # 99th percentile: score 0 at the first value, 1 at the second
OVEREXPOSED_P5 = (240.0, 200.0)  # 5th percentile: score 0 at the first value, 1 at the second
FULL_CONTRAST_RANGE = 90.0  # p99 - p1 for a contrast score of 1
BLACK_P99 = 20.0  # a frame whose 99th percentile is below this is black
WEIGHTS = {"sharpness": 0.5, "exposure": 0.3, "contrast": 0.2}


@dataclass(frozen=True)
class FrameQuality:
    sharpness: float
    exposure: float
    contrast: float
    is_black: bool
    technical_score: float
    laplacian_variance: float
    mean_luma: float


@dataclass(frozen=True)
class SegmentQuality:
    """Quality of a candidate: the mean over its sampled frames."""

    sharpness: float
    exposure: float
    contrast: float
    black_fraction: float
    technical_score: float
    frames: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_frames(cls, frames: list[FrameQuality]) -> "SegmentQuality":
        if not frames:
            raise ValueError("a segment needs at least one frame")
        return cls(
            sharpness=_mean(f.sharpness for f in frames),
            exposure=_mean(f.exposure for f in frames),
            contrast=_mean(f.contrast for f in frames),
            black_fraction=_mean(float(f.is_black) for f in frames),
            technical_score=_mean(f.technical_score for f in frames),
            frames=len(frames),
        )


def measure_frame(gray: np.ndarray) -> FrameQuality:
    """Measure one grayscale uint8 frame."""
    if gray.ndim != 2 or gray.dtype != np.uint8:
        raise ValueError("expected a 2-D uint8 grayscale frame")

    mean = float(gray.mean())
    variance = float(gray.var())
    laplacian_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    p1, p5, p99 = _percentiles(gray, (1, 5, 99))

    reliability = min(1.0, variance**0.5 / CONTRAST_STD_FOR_RELIABLE_SHARPNESS)
    relative = laplacian_variance / (variance + LUMA_VARIANCE_FLOOR)
    measured = 1.0 - float(np.exp(-relative / SHARPNESS_SCALE))
    sharpness = reliability * measured + (1.0 - reliability) * NEUTRAL_SHARPNESS

    exposure = min(_ramp(p99, *UNDEREXPOSED_P99), _ramp(p5, *OVEREXPOSED_P5))
    contrast = float(np.clip((p99 - p1) / FULL_CONTRAST_RANGE, 0.0, 1.0))
    is_black = p99 < BLACK_P99

    score = 0.0 if is_black else _weighted_geometric_mean(
        {"sharpness": sharpness, "exposure": exposure, "contrast": contrast})
    return FrameQuality(sharpness, exposure, contrast, is_black, score, laplacian_variance, mean)


def _percentiles(gray: np.ndarray, qs: tuple[float, ...]) -> list[float]:
    histogram = np.bincount(gray.ravel(), minlength=256)
    cumulative = np.cumsum(histogram) / gray.size
    return [float(np.searchsorted(cumulative, q / 100.0)) for q in qs]


def _ramp(value: float, zero_at: float, one_at: float) -> float:
    """Linear 0-1 ramp between two points, in either direction."""
    return float(np.clip((value - zero_at) / (one_at - zero_at), 0.0, 1.0))


def _weighted_geometric_mean(parts: dict[str, float]) -> float:
    """Any very poor aspect pulls the score down; a plain mean would hide a blurred shot."""
    score = 1.0
    for name, value in parts.items():
        score *= max(value, 0.0) ** WEIGHTS[name]
    return float(score)


def _mean(values) -> float:
    values = list(values)
    return float(sum(values) / len(values))
