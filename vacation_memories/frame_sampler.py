"""Decode photos and clips once and hand analysis frames to the quality and duplicate steps.

A clip is read in a single sequential pass. Every decoded frame is grabbed, but converted only when
it is needed: a few frames per second feed the scene detector and the motion estimate, and one
frame every `sample_interval` seconds is turned into the grayscale analysis frame and passed to the
`analyze` callback. Only the callback's small result is kept, never the frames.

Timestamps are the decoder's own (`CAP_PROP_POS_MSEC`), relative to the first frame.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import cv2
import numpy as np
from PIL import Image, ImageOps

from vacation_memories.media_scanner import heic_available
from vacation_memories.scene_detector import SceneCutDetector

logger = logging.getLogger(__name__)

T = TypeVar("T")

ANALYSIS_LONG_SIDE = 512  # all quality measures and hashes use frames of this size
DETECTION_FPS = 10.0  # frames per second given to the scene detector and to the motion estimate
DETECTION_WIDTH = 320
MOTION_WIDTH = 64
SAMPLE_INTERVAL_SECONDS = 0.5  # shorter than the minimum scene length: every scene gets a sample


class SamplingError(Exception):
    """The file cannot be decoded."""


@dataclass(frozen=True)
class VideoScan(Generic[T]):
    """Result of the single pass over a clip."""

    duration: float
    cut_times: list[float]
    samples: list[tuple[float, T]]  # (timestamp, result of `analyze`)
    motion: list[tuple[float, float]]  # (timestamp, mean luma change from the previous frame, 0-1)


def to_analysis_gray(image: np.ndarray, long_side: int = ANALYSIS_LONG_SIDE) -> np.ndarray:
    """Grayscale uint8 copy whose longer side is `long_side`, whatever the source size."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    scale = long_side / max(height, width)
    if scale == 1:
        return gray
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)


def photo_analysis_frame(path: Path) -> np.ndarray:
    """Analysis frame of a photo, upright according to its EXIF orientation."""
    heic_available()  # registers the HEIC decoder when present
    try:
        with Image.open(path) as image:
            image.draft("L", (ANALYSIS_LONG_SIDE * 2, ANALYSIS_LONG_SIDE * 2))
            upright = ImageOps.exif_transpose(image)
            gray = np.asarray(upright.convert("L"))
    except Exception as error:  # Pillow raises many types on corrupt data
        raise SamplingError(f"unreadable photo: {error}") from error
    return to_analysis_gray(gray)


def photo_color_frame(path: Path) -> Image.Image:
    """Full-size RGB image of a photo, upright according to its EXIF orientation (for the vision model)."""
    heic_available()
    try:
        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).convert("RGB")
    except Exception as error:
        raise SamplingError(f"unreadable photo: {error}") from error


def video_color_frame(path: Path, timestamp: float) -> Image.Image:
    """RGB frame of a clip at `timestamp` seconds from its first frame, upright (for the vision model)."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise SamplingError("cannot open the clip")
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
        if not capture.grab():
            raise SamplingError("no decodable frames")
        first_time = capture.get(cv2.CAP_PROP_POS_MSEC)  # the decoder's own clock does not always start at 0
        capture.set(cv2.CAP_PROP_POS_MSEC, first_time + timestamp * 1000.0)
        ok, frame = capture.read()
        if not ok:
            raise SamplingError(f"cannot decode the frame at {timestamp:.2f}s")
    finally:
        capture.release()
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def scan_video(
    path: Path,
    analyze: Callable[[np.ndarray], T],
    *,
    sample_interval: float = SAMPLE_INTERVAL_SECONDS,
    detector_factory: Callable[[float], SceneCutDetector] = SceneCutDetector,
) -> VideoScan[T]:
    """Decode a clip once: scene cuts, motion and `analyze(frame)` every `sample_interval` seconds."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise SamplingError("cannot open the clip")
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0 or fps != fps:
            raise SamplingError("unknown frame rate")
        return _scan(path, capture, fps, analyze, sample_interval, detector_factory(fps))
    finally:
        capture.release()


def _scan(path, capture, fps, analyze, sample_interval, detector) -> VideoScan:
    stride = max(1, round(fps / DETECTION_FPS))
    detection_times: dict[int, float] = {}
    samples: list[tuple[float, T]] = []
    motion: list[tuple[float, float]] = []
    previous_small = None
    first_time = None
    last_time = 0.0
    next_sample = 0.0
    index = 0

    while capture.grab():
        timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if first_time is None:
            first_time = timestamp
        timestamp -= first_time
        if index and timestamp <= last_time:  # decoders that do not report usable timestamps
            timestamp = last_time + 1.0 / fps
        last_time = timestamp

        detect = index % stride == 0
        sample = timestamp >= next_sample - 1e-6
        if detect or sample:
            ok, frame = capture.retrieve()
            if not ok:
                logger.warning("%s: undecodable frame at %.2fs, stopping the scan", path, timestamp)
                break
            if detect:
                detection_times[index] = timestamp
                detector.feed(index, _resize_to_width(frame, DETECTION_WIDTH))
                small = _resize_to_width(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), MOTION_WIDTH).astype(np.int16)
                if previous_small is not None:
                    change = float(np.abs(small - previous_small).mean()) / 255.0
                    motion.append((timestamp, change))
                previous_small = small
            if sample:
                samples.append((timestamp, analyze(to_analysis_gray(frame))))
                next_sample = timestamp + sample_interval
        index += 1

    if not samples:
        raise SamplingError("no decodable frames")
    cut_times = sorted({detection_times[i] for i in detector.cut_frames if i in detection_times})
    return VideoScan(duration=last_time + 1.0 / fps, cut_times=cut_times, samples=samples, motion=motion)


def _resize_to_width(image: np.ndarray, width: int) -> np.ndarray:
    height, current = image.shape[:2]
    if current <= width:
        return image
    return cv2.resize(image, (width, max(1, round(height * width / current))), interpolation=cv2.INTER_AREA)
