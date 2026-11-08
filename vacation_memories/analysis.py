"""Scanning and analysis of a media folder, shared by the pipeline and the evaluation.

Metadata, technical analysis, technical threshold, near-duplicates, AI analysis of the survivors and
ranking, all through the cache of the working folder: a second analysis of the same material with
the same parameters reads everything from the cache. Ollama is contacted only for the frames whose
answers are not cached yet.
"""

import json
import logging
import math
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from vacation_memories.atomic import atomic_write_json
from vacation_memories.cache_manager import CacheManager
from vacation_memories.config import Settings
from vacation_memories.duplicate_detector import DuplicateResult, HashBackend, find_duplicates
from vacation_memories.edit_planner import estimated_clip_count
from vacation_memories.media_scanner import scan_media
from vacation_memories.metadata_extractor import MediaMetadata, load_metadata
from vacation_memories.ollama_client import OllamaClient
from vacation_memories.process import ShutdownController, ToolRunner
from vacation_memories.renderer import Progress
from vacation_memories.runtime_profile import (
    DetectionEnv,
    detect_ollama,
    loaded_model_placement,
    record_model_placement,
)
from vacation_memories.scene_ranker import Discarded, Ranking, rank_candidates, technical_filter
from vacation_memories.technical_analysis import Candidate, analyze_media
from vacation_memories.vision_analyzer import AiCoverage, VisionAnalyzer, analyze_candidates, prioritize
from vacation_memories.vision_schema import VisionResult

logger = logging.getLogger(__name__)

RUNTIME_PROFILE_NAME = "runtime_profile.json"


class RequirementsError(Exception):
    """Something the run needs is missing (exit code 4)."""


@dataclass(frozen=True)
class AnalysisRequest:
    """What decides the result of an analysis."""

    input: Path
    workdir: Path
    duration: float  # the target of the edit, which sizes the AI budget
    assume_tz: str | None
    no_ai: bool
    model: str
    ai_resolution: int
    ai_frames: int | None
    ai_time_budget: float | None


@dataclass
class Analysis:
    ranking: Ranking  # the candidates above the technical threshold that represent their duplicate group
    coverage: AiCoverage | None  # None without AI
    counts: dict
    metadata: dict[Path, MediaMetadata]  # of every readable file
    candidates: list[Candidate]  # every candidate of the readable files, discarded ones included
    ai_results: dict[str, VisionResult]
    duplicates: DuplicateResult  # the grouping of the candidates above the technical threshold

    @cached_property
    def duplicates_of_everything(self) -> DuplicateResult:
        """The grouping, and the similarities, of every candidate of the readable files."""
        return find_duplicates([candidate.duplicate_item() for candidate in self.candidates], HashBackend())

    def ranking_of_everything(self, config) -> Ranking:
        """Every candidate of the readable files, whatever its quality or duplicates (for the random baseline)."""
        return rank_candidates(self.candidates, self.metadata, self.duplicates_of_everything, self.ai_results, config,
                               ai_used=bool(self.ai_results))

    def ranking_without_ai(self, config) -> Ranking:
        """The same candidates as `ranking`, scored as if the vision model had not answered."""
        survivors = [item.candidate for item in self.ranking.ranked]
        ranking = rank_candidates(survivors, self.metadata, self.duplicates, {}, config, ai_used=False)
        ranking.discarded = list(self.ranking.discarded)
        return ranking


def run_analysis(
    request: AnalysisRequest,
    settings: Settings,
    *,
    controller: ShutdownController,
    progress: Progress,
    env: DetectionEnv,
) -> Analysis:
    """Analyze the folder of `request` with the selection configuration and AI options of `settings`."""
    config = settings.selection
    progress.phase("scanning", 0)
    files = scan_media(request.input)
    backend = HashBackend()
    with CacheManager(request.workdir) as cache:
        progress.phase("reading metadata", len(files))
        loaded = load_metadata(files, cache, request.assume_tz, check=controller.check,
                               progress=progress.advance, runner=ToolRunner(controller))
        metadata = {media.rel_path: info for media, info in loaded}
        readable = [media for media, _ in loaded]

        progress.phase("technical analysis", len(readable))
        candidates = analyze_media(readable, backend, cache=cache, check=controller.check, progress=progress.advance)
        passed, discarded = technical_filter(candidates, config)
        duplicates = find_duplicates([candidate.duplicate_item() for candidate in passed], backend)
        representatives = duplicates.representatives()
        survivors = [candidate for candidate in passed if candidate.id in representatives]
        for group in duplicates.duplicate_groups:
            discarded += [Discarded(member.id, "duplicate", member.reason) for member in group.duplicates]
        logger.info(
            "analysis: %d file(s), %d candidate(s), %d below the technical threshold, %d duplicate(s), "
            "%d left", len(files), len(candidates), len(candidates) - len(passed),
            len(passed) - len(survivors), len(survivors),
        )
        ai_results, coverage = _ai_phase(request, settings, survivors, readable, cache, controller, progress, env)

    ranking = rank_candidates(survivors, metadata, duplicates, ai_results, config, ai_used=bool(ai_results))
    ranking.discarded = discarded
    counts = {
        "files": len(files),
        "readable_files": len(readable),
        "candidates": len(candidates),
        "below_technical_threshold": len(candidates) - len(passed),
        "duplicates": len(passed) - len(survivors),
        "eligible": len(survivors),
    }
    return Analysis(ranking, coverage, counts, metadata, candidates, ai_results, duplicates)


def _ai_phase(request, settings, survivors, readable, cache, controller, progress, env):
    if request.no_ai:
        logger.info("AI analysis skipped (--no-ai)")
        return {}, None
    client = OllamaClient(settings.ollama_url, request.model, timeout=settings.ai_timeout)
    analyzer = VisionAnalyzer(client, model=request.model, resolution=request.ai_resolution, cache=cache,
                              unload_model=settings.ai_unload_model)
    ordered = prioritize(survivors)
    if request.ai_frames is not None:
        selected, frames, budget = ordered[: request.ai_frames], request.ai_frames, None
    else:
        cap = math.ceil(settings.selection.ai_frames_per_clip * estimated_clip_count(request.duration,
                                                                                      settings.selection))
        selected, frames, budget = ordered[:cap], None, request.ai_time_budget
    media_by_path = {media.rel_path: media for media in readable}

    cached = {}
    for candidate in selected:
        media = media_by_path[candidate.rel_path]
        timestamp = None if candidate.start is None else (candidate.start + candidate.end) / 2
        if (result := analyzer.lookup(media, timestamp)) is not None:
            cached[candidate.id] = result
    if len(cached) == len(selected):
        logger.info("AI analysis: all %d frame(s) already in the cache, Ollama is not contacted", len(cached))
        coverage = AiCoverage(model=request.model, mode="cache", limit=len(selected), candidates=len(survivors),
                              selected=len(selected), analyzed=len(cached), from_cache=len(cached))
        return cached, coverage

    ollama = detect_ollama(env, settings.ollama_url, request.model, logger.warning)
    _record_ollama(request.workdir, ollama.to_dict())
    if not ollama.reachable or not ollama.model_present:
        problem = (f"Ollama is not reachable at {settings.ollama_url}" if not ollama.reachable
                   else f"the model {request.model} is not installed in Ollama (see `ollama list`)")
        if settings.require_ai:
            raise RequirementsError(f"{problem}, and --require-ai is set")
        logger.warning("%s: continuing without AI for the %d frame(s) not in the cache", problem,
                       len(selected) - len(cached))
        coverage = AiCoverage(model=request.model, mode="unavailable", limit=len(selected),
                              candidates=len(survivors), selected=len(selected), analyzed=len(cached),
                              from_cache=len(cached))
        return cached, coverage

    controller.on_shutdown(client.abort)
    progress.phase("AI analysis", len(selected))
    with analyzer:
        outcome = analyze_candidates(
            selected, media_by_path, analyzer, frames=frames, time_budget=budget,
            max_concurrency=settings.ai_max_concurrency,
            probe_placement=lambda: loaded_model_placement(env, settings.ollama_url, request.model),
            progress=lambda done, total: progress.advance_to(done), stop=lambda: controller.requested,
        )
    controller.check()
    outcome.coverage.candidates = len(survivors)
    record_model_placement(request.workdir / RUNTIME_PROFILE_NAME, outcome.coverage.placement)
    if settings.require_ai and selected and not outcome.results:
        raise RequirementsError("the vision model gave no usable answer, and --require-ai is set")
    return outcome.results, outcome.coverage


def _record_ollama(workdir: Path, info: dict) -> None:
    path = workdir / RUNTIME_PROFILE_NAME
    try:
        data = json.loads(path.read_text())
        data["ollama"] = info
        atomic_write_json(path, data)
    except (OSError, ValueError):
        logger.warning("cannot record the Ollama state in %s", path)
