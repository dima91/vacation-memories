"""Scores of the candidates: technical threshold, `narrative_score` and the final score (section 6).

The two dimensions are computed and kept apart:

- `technical_score` (0-1) comes from the technical analysis (section 3) and only says how well the
  shot turned out. Below `technical_threshold` a candidate is discarded, whatever it shows.
- `narrative_score` (0-1) says how worthwhile it is to tell the moment. Its components are the AI's
  `narrative_value`, uniqueness, contribution to variety (`category`, `moment_type`), motion and
  temporal distribution. A candidate without an AI answer (no AI, or outside the AI budget) uses
  only the non-semantic ones: uniqueness, motion and temporal distribution.

Above the threshold the final score is a weighted sum with more weight on the narrative, so a unique,
slightly imperfect moment can beat a perfect but banal shot. The redundancy penalty depends on what
is already chosen and is applied by the planner.
"""

import bisect
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from vacation_memories.duplicate_detector import DuplicateResult
from vacation_memories.metadata_extractor import MediaMetadata
from vacation_memories.selection_config import SelectionConfig
from vacation_memories.technical_analysis import Candidate
from vacation_memories.vision_schema import VisionResult

UNRELATED_SIMILARITY = 0.5  # hash similarity of two unrelated images, on average
BELOW_THRESHOLD = "below_technical_threshold"
MOSTLY_BLACK = "mostly_black"


@dataclass(frozen=True)
class RankedCandidate:
    """A candidate that passed the technical threshold and the duplicate grouping, with its scores."""

    candidate: Candidate
    captured_at: datetime  # naive, as written in the metadata, plus the offset of the segment in its clip
    date_source: str
    assumed_tz: str | None
    ai: VisionResult | None
    technical_score: float
    narrative_score: float
    final_score: float
    motion_level: float  # 0 (still) to 1 (fully dynamic); a photo has a neutral value
    components: dict[str, float] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.candidate.id

    @property
    def day(self) -> date:
        return self.captured_at.date()

    @property
    def moment_type(self) -> str | None:
        return self.ai.moment_type if self.ai else None

    @property
    def category(self) -> str | None:
        return self.ai.category if self.ai else None

    @property
    def narrative_value(self) -> int | None:
        return self.ai.narrative_value if self.ai else None

    @property
    def available_seconds(self) -> float | None:
        """Length of the clip segment; None for a photo, which can last any time."""
        if self.candidate.start is None:
            return None
        return self.candidate.end - self.candidate.start


@dataclass(frozen=True)
class Discarded:
    id: str
    reason: str
    detail: str


@dataclass
class Ranking:
    ranked: list[RankedCandidate]  # chronological
    similarity: np.ndarray  # hash similarity between the ranked candidates, in the same order
    ai_used: bool
    notes: list[str] = field(default_factory=list)
    discarded: list[Discarded] = field(default_factory=list)


def technical_filter(
    candidates: list[Candidate], config: SelectionConfig
) -> tuple[list[Candidate], list[Discarded]]:
    """Rule 1: below the threshold (blurred, black, badly exposed) a candidate is out, whatever it shows."""
    passed, discarded = [], []
    for candidate in candidates:
        quality = candidate.quality
        if quality.black_fraction > config.max_black_fraction:
            discarded.append(Discarded(candidate.id, MOSTLY_BLACK, f"black for {quality.black_fraction:.0%} of the frames"))
        elif quality.technical_score < config.technical_threshold:
            discarded.append(Discarded(
                candidate.id, BELOW_THRESHOLD,
                f"technical_score {quality.technical_score:.2f} below {config.technical_threshold:.2f}",
            ))
        else:
            passed.append(candidate)
    return passed, discarded


def capture_time(candidate: Candidate, metadata: MediaMetadata) -> datetime:
    return metadata.capture_datetime + timedelta(seconds=candidate.start or 0.0)


def rank_candidates(
    survivors: list[Candidate],
    metadata: Mapping[Path, MediaMetadata],
    duplicates: DuplicateResult,
    ai_results: Mapping[str, VisionResult],
    config: SelectionConfig,
    *,
    ai_used: bool,
) -> Ranking:
    """Scores of the candidates that passed the technical threshold and represent their duplicate group.

    `duplicates` is the grouping of (at least) these candidates; its similarity matrix gives the
    uniqueness and, later, the redundancy penalty.
    """
    survivors = [candidate for candidate in survivors if candidate.rel_path in metadata]
    survivors.sort(key=lambda c: (capture_time(c, metadata[c.rel_path]), c.id))
    times = [capture_time(candidate, metadata[candidate.rel_path]) for candidate in survivors]
    index = {candidate_id: position for position, candidate_id in enumerate(duplicates.ids)}
    positions = [index[candidate.id] for candidate in survivors]
    matrix = duplicates.similarity[np.ix_(positions, positions)] if positions else np.zeros((0, 0))

    uniqueness = _uniqueness(matrix)
    temporal = _temporal(times, config.temporal_window_minutes)
    analyzed = [ai_results[c.id] for c in survivors if c.id in ai_results]
    rarity = _rarity(analyzed)

    ranked = []
    for number, candidate in enumerate(survivors):
        info = metadata[candidate.rel_path]
        ai = ai_results.get(candidate.id)
        motion_level = _motion_level(candidate, config)
        components = {"uniqueness": uniqueness[number], "motion": motion_level, "temporal": temporal[number]}
        if ai is not None and ai.narrative_value is not None:
            components["ai_value"] = (ai.narrative_value - 1) / 4
        if ai is not None and (variety := rarity(ai)) is not None:
            components["variety"] = variety
        narrative = _weighted(components, config)
        technical = candidate.technical_score
        final = config.narrative_weight * narrative + config.technical_weight * technical
        ranked.append(RankedCandidate(
            candidate=candidate, captured_at=times[number], date_source=info.date_source,
            assumed_tz=info.assumed_tz, ai=ai, technical_score=technical, narrative_score=narrative,
            final_score=final, motion_level=motion_level,
            components={name: round(value, 4) for name, value in components.items()},
        ))

    notes = []
    if not ai_used:
        notes.append(
            "without AI: narrative_score uses only uniqueness, motion and temporal distribution, and the criteria "
            "based on moment_type (scenery at the start of a day, calm scenes for the Closing) fall back on motion "
            "and non-semantic scores"
        )
    elif len(analyzed) < len(ranked):
        notes.append(
            f"partial AI coverage: {len(analyzed)} of {len(ranked)} candidates analyzed; the others use only the "
            "non-semantic components"
        )
    return Ranking(ranked, matrix, ai_used, notes)


def _weighted(components: dict[str, float], config: SelectionConfig) -> float:
    weights = {
        "ai_value": config.ai_value_weight,
        "uniqueness": config.uniqueness_weight,
        "variety": config.variety_weight,
        "motion": config.motion_weight,
        "temporal": config.temporal_weight,
    }
    total = sum(weights[name] for name in components)
    if total <= 0:
        return 0.0
    return sum(weights[name] * value for name, value in components.items()) / total


def _uniqueness(matrix: np.ndarray) -> list[float]:
    """1 for a candidate unlike any other, 0 for one with a twin (its closest match decides)."""
    count = len(matrix)
    if count < 2:
        return [1.0] * count
    others = matrix.copy()
    np.fill_diagonal(others, 0.0)
    closest = others.max(axis=1)
    return [float(np.clip((1.0 - value) / (1.0 - UNRELATED_SIMILARITY), 0.0, 1.0)) for value in closest]


def _temporal(times: list[datetime], window_minutes: float) -> list[float]:
    """Higher for candidates from quiet stretches: 1 at the median density, lower in crowded moments."""
    if not times:
        return []
    window = timedelta(minutes=window_minutes)
    counts = [bisect.bisect_right(times, t + window) - bisect.bisect_left(times, t - window) for t in times]
    typical = float(np.median(counts))
    return [min(1.0, math.sqrt(typical / count)) for count in counts]


def _rarity(analyzed: list[VisionResult]):
    """Contribution to variety: how rare the candidate's category and moment type are among the analyzed ones."""
    categories = Counter(result.category for result in analyzed if result.category)
    moments = Counter(result.moment_type for result in analyzed if result.moment_type)

    def rarity(result: VisionResult) -> float | None:
        values = []
        if result.category and categories:
            values.append(1.0 - categories[result.category] / sum(categories.values()))
        if result.moment_type and moments:
            values.append(1.0 - moments[result.moment_type] / sum(moments.values()))
        return sum(values) / len(values) if values else None

    return rarity


def _motion_level(candidate: Candidate, config: SelectionConfig) -> float:
    if candidate.kind == "photo":
        return config.photo_motion
    if candidate.motion is None:
        return 0.0
    return float(min(1.0, candidate.motion / config.full_motion))
