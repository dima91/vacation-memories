"""Synthetic candidates with known scores, for the ranking and planning tests (no media involved)."""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from vacation_memories.metadata_extractor import MediaMetadata
from vacation_memories.quality_analyzer import SegmentQuality
from vacation_memories.scene_ranker import RankedCandidate, Ranking
from vacation_memories.technical_analysis import Candidate
from vacation_memories.vision_schema import VisionResult

START = datetime(2023, 7, 14, 8, 0, 0)


def candidate(name: str, *, technical: float = 0.8, kind: str = "photo", start: float | None = None,
              end: float | None = None, motion: float | None = None, black: float = 0.0, seed: int = 0) -> Candidate:
    quality = SegmentQuality(sharpness=technical, exposure=technical, contrast=technical, black_fraction=black,
                             technical_score=technical, frames=1)
    rel = Path(name)
    cid = name if start is None else f"{name}#{start:.3f}-{end:.3f}"
    signature = np.random.RandomState(seed).randint(0, 256, (1, 8)).astype(np.uint8)
    return Candidate(cid, rel, kind, start, end, quality, motion, signature)


def metadata(when: datetime, kind: str = "photo") -> MediaMetadata:
    return MediaMetadata(kind, 1920, 1080, when, "exif" if kind == "photo" else "container",
                         duration=None if kind == "photo" else 30.0)


def vision(moment: str | None = None, value: int | None = 3, category: str | None = "other") -> VisionResult:
    return VisionResult("a scene", category, moment, value, False)


def ranked(name: str, when: datetime, *, moment: str | None = None, value: int | None = None,
           category: str | None = None, narrative: float = 0.5, technical: float = 0.8, kind: str = "photo",
           seconds: float = 8.0, motion_level: float = 0.3) -> RankedCandidate:
    start, end = (None, None) if kind == "photo" else (0.0, seconds)
    item = candidate(name, technical=technical, kind=kind, start=start, end=end)
    ai = None if moment is None and value is None else vision(moment, value, category or "other")
    final = 0.65 * narrative + 0.35 * technical
    return RankedCandidate(item, when, "exif", None, ai, technical, narrative, final, motion_level,
                           {"uniqueness": narrative})


def ranking_of(items: list[RankedCandidate], *, ai_used: bool = True) -> Ranking:
    items = sorted(items, key=lambda item: (item.captured_at, item.id))
    similarity = np.full((len(items), len(items)), 0.5, dtype=np.float32)
    np.fill_diagonal(similarity, 1.0)
    return Ranking(items, similarity, ai_used)


def vacation(count: int, *, days: int = 3, weights: list[float] | None = None, moments=None, seed: int = 0,
             videos_every: int = 0) -> list[RankedCandidate]:
    """`count` candidates over `days` days (in proportion to `weights`), with cycling moment types."""
    rng = np.random.RandomState(seed)
    weights = weights or [1.0] * days
    per_day = [round(count * w / sum(weights)) for w in weights]
    per_day[0] += count - sum(per_day)
    items = []
    number = 0
    for day, amount in enumerate(per_day):
        for slot in range(amount):
            when = START + timedelta(days=day, hours=12 * (slot + 0.5) / amount)
            moment = None if moments is None else moments[number % len(moments)]
            kind = "video" if videos_every and number % videos_every == 0 else "photo"
            items.append(ranked(
                f"day{day + 1}/item{number:04d}.{'mp4' if kind == 'video' else 'jpg'}", when, moment=moment,
                value=None if moment is None else int(rng.randint(2, 6)), narrative=float(rng.uniform(0.2, 0.9)),
                technical=float(rng.uniform(0.4, 1.0)), kind=kind, motion_level=float(rng.uniform(0, 1)),
            ))
            number += 1
    return items
