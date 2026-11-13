"""Scene cuts (PySceneDetect) and candidate segments with precise timestamps.

`SceneCutDetector` is fed frame by frame by `frame_sampler`, so a clip is decoded only once for
cuts, motion and sampling. `build_segments` turns the cuts into candidate segments and splits long
scenes (or clips without cuts) into windows, which is the temporal sampling of continuous videos.
"""

import math
from dataclasses import dataclass

import numpy as np
from scenedetect.detectors import ContentDetector

CUT_THRESHOLD = 35.0  # above PySceneDetect's default (27): only obvious cuts, fast motion must not split a scene
MIN_SCENE_SECONDS = 0.6
MAX_SEGMENT_SECONDS = 10.0


@dataclass(frozen=True)
class TimeSpan:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class SceneCutDetector:
    """Streaming wrapper around PySceneDetect's `ContentDetector`.

    Frames may be fed with a stride: `frame_index` is the real index in the clip, and the minimum
    scene length is expressed in real frames.
    """

    def __init__(self, fps: float, threshold: float = CUT_THRESHOLD, min_scene_seconds: float = MIN_SCENE_SECONDS):
        min_frames = max(1, round(min_scene_seconds * fps))
        self._detector = ContentDetector(threshold=threshold, min_scene_len=min_frames)
        self.cut_frames: list[int] = []

    def feed(self, frame_index: int, frame_bgr: np.ndarray) -> None:
        """Process the next frame; a cut is the index of the first frame of a new scene."""
        self.cut_frames.extend(self._detector.process_frame(frame_index, frame_bgr))


def build_segments(
    cut_times: list[float], start: float, end: float, max_segment_seconds: float = MAX_SEGMENT_SECONDS
) -> list[TimeSpan]:
    """Candidate segments covering `[start, end]`: one per scene, long scenes split evenly."""
    if end <= start:
        raise ValueError("the clip has no duration")
    boundaries = [start] + sorted({t for t in cut_times if start < t < end}) + [end]
    segments = []
    for scene_start, scene_end in zip(boundaries, boundaries[1:]):
        pieces = max(1, math.ceil((scene_end - scene_start) / max_segment_seconds - 1e-9))
        step = (scene_end - scene_start) / pieces
        for n in range(pieces):
            segments.append(TimeSpan(scene_start + n * step, scene_end if n == pieces - 1 else scene_start + (n + 1) * step))
    return segments
