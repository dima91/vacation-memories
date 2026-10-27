"""Technical analysis of the media: candidate segments with quality and duplicate signatures.

For each photo there is one candidate. Each clip is decoded once (see `frame_sampler`), split into
scenes and windows (see `scene_detector`) and every segment becomes a candidate with its sampled
frames' quality aggregated into a `technical_score`. Candidates are then grouped by
`duplicate_detector`; nothing here touches the files.

The candidates of each file are cached, so a new run (another `--duration`, structure or output
format) does not analyze the media again.
"""

import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from vacation_memories.cache_manager import CacheManager
from vacation_memories.duplicate_detector import DuplicateItem, SimilarityBackend
from vacation_memories.frame_sampler import (
    SAMPLE_INTERVAL_SECONDS,
    SamplingError,
    VideoScan,
    photo_analysis_frame,
    scan_video,
)
from vacation_memories.media_scanner import MediaFile
from vacation_memories.quality_analyzer import FrameQuality, SegmentQuality, measure_frame
from vacation_memories.scene_detector import MAX_SEGMENT_SECONDS, TimeSpan, build_segments

logger = logging.getLogger(__name__)

HASH_FRAMES_PER_SEGMENT = 3
ANALYSIS_VERSION = 2  # bump when the analysis changes: cached candidates are recomputed


@dataclass(frozen=True)
class Candidate:
    """A photo, or a segment `[start, end)` of a clip (seconds from its first frame)."""

    id: str
    rel_path: Path
    kind: str  # "photo" or "video"
    start: float | None
    end: float | None
    quality: SegmentQuality
    motion: float | None  # median change between consecutive frames, 0-1 (clips only)
    signatures: np.ndarray

    @property
    def technical_score(self) -> float:
        return self.quality.technical_score

    def duplicate_item(self, narrative_score: float | None = None) -> DuplicateItem:
        return DuplicateItem(self.id, self.signatures, self.technical_score, narrative_score)

    def to_cache(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "quality": self.quality.to_dict(),
            "motion": self.motion,
            "signatures": [row.tobytes().hex() for row in self.signatures],
        }

    @classmethod
    def from_cache(cls, media: MediaFile, data: dict) -> "Candidate":
        start, end = data["start"], data["end"]
        signatures = np.stack([np.frombuffer(bytes.fromhex(row), dtype=np.uint8) for row in data["signatures"]])
        return cls(_candidate_id(media, start, end), media.rel_path, media.kind, start, end,
                   SegmentQuality(**data["quality"]), data["motion"], signatures)


def analysis_params(
    backend: SimilarityBackend, sample_interval: float, max_segment_seconds: float, hash_frames: int
) -> str:
    """What changes the candidates of a file, and so belongs in the cache key."""
    signature = getattr(backend, "signature_params", backend.name)
    return (f"version={ANALYSIS_VERSION}|signature={signature}|interval={sample_interval:g}"
            f"|max_segment={max_segment_seconds:g}|hash_frames={hash_frames}")


def analyze_media(
    files: list[MediaFile],
    backend: SimilarityBackend,
    *,
    sample_interval: float = SAMPLE_INTERVAL_SECONDS,
    max_segment_seconds: float = MAX_SEGMENT_SECONDS,
    hash_frames: int = HASH_FRAMES_PER_SEGMENT,
    cache: CacheManager | None = None,
    check: Callable[[], None] | None = None,
    progress: Callable[[], None] | None = None,
) -> list[Candidate]:
    """Candidates of every readable file, in the order of `files`; unreadable files are logged and skipped.

    `check` is called between files and during long decodes, and may raise to stop the analysis;
    `progress` is called after every file.
    """
    params = analysis_params(backend, sample_interval, max_segment_seconds, hash_frames)
    candidates: list[Candidate] = []
    for media in files:
        if check:
            check()
        cached = cache.get_technical(media, params) if cache else None
        if cached is not None:
            candidates.extend(Candidate.from_cache(media, item) for item in cached)
        else:
            try:
                if media.kind == "photo":
                    found = [_photo_candidate(media, backend)]
                else:
                    found = _video_candidates(media, backend, sample_interval, max_segment_seconds, hash_frames, check)
            except SamplingError as error:
                logger.warning("skipping %s: %s", media.rel_path, error)
                found = None
            if found is not None:
                if cache:
                    cache.put_technical(media, params, [candidate.to_cache() for candidate in found])
                candidates.extend(found)
        if progress:
            progress()
    return candidates


def _candidate_id(media: MediaFile, start: float | None, end: float | None) -> str:
    path = media.rel_path.as_posix()
    return path if start is None else f"{path}#{start:.3f}-{end:.3f}"


def _photo_candidate(media: MediaFile, backend: SimilarityBackend) -> Candidate:
    gray = photo_analysis_frame(media.path)
    quality = measure_frame(gray)
    return Candidate(_candidate_id(media, None, None), media.rel_path, "photo", None, None,
                     SegmentQuality.from_frames([quality]), None, backend.signature(gray)[None, :])


def _video_candidates(
    media: MediaFile, backend: SimilarityBackend, sample_interval: float, max_segment_seconds: float, hash_frames: int,
    check: Callable[[], None] | None = None,
) -> list[Candidate]:
    scan = scan_video(media.path, lambda gray: (measure_frame(gray), backend.signature(gray)),
                      sample_interval=sample_interval, check=check)
    candidates = []
    for span in build_segments(scan.cut_times, 0.0, scan.duration, max_segment_seconds):
        samples = _samples_in(scan, span)
        qualities: list[FrameQuality] = [quality for _, (quality, _) in samples]
        chosen = _evenly_spaced(len(samples), hash_frames)
        signatures = np.stack([samples[i][1][1] for i in chosen])
        candidates.append(Candidate(
            id=_candidate_id(media, span.start, span.end),
            rel_path=media.rel_path, kind="video", start=span.start, end=span.end,
            quality=SegmentQuality.from_frames(qualities), motion=_motion_in(scan, span), signatures=signatures,
        ))
    return candidates


def _samples_in(scan: VideoScan, span: TimeSpan) -> list:
    inside = [sample for sample in scan.samples if span.start - 1e-6 <= sample[0] < span.end]
    if inside:
        return inside
    middle = (span.start + span.end) / 2
    return [min(scan.samples, key=lambda sample: abs(sample[0] - middle))]


def _motion_in(scan: VideoScan, span: TimeSpan) -> float | None:
    """Median change inside the segment; the frame that opens it spans the cut and is left out."""
    values = [value for t, value in scan.motion if span.start < t < span.end]
    return statistics.median(values) if values else None


def _evenly_spaced(count: int, wanted: int) -> list[int]:
    if count <= wanted:
        return list(range(count))
    return sorted({round(i) for i in np.linspace(0, count - 1, wanted)})
