"""Semantic analysis of the candidates with the local vision model (optional, survivors only).

The caller passes the candidates that passed the technical filters and the duplicate grouping. They
are taken in priority order (best technical score first), so partial coverage is still useful: the
frames that are not analyzed, or whose analysis fails, simply keep their non-semantic scores.

How many frames are analyzed is decided at runtime. A short warm-up measures the seconds per frame
of the machine in use; with a time budget the number of frames follows from it, while a fixed number
of frames (`frames`) gives the same selection on any machine. Frames already in the cache cost
nothing and do not use the budget. Answers are cached per (file, timestamp, model, prompt,
resolution); failures are not.
"""

import base64
import io
import json
import logging
import statistics
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from vacation_memories.cache_manager import CacheManager
from vacation_memories.config import DEFAULT_AI_TIME_BUDGET_SECONDS
from vacation_memories.frame_sampler import SamplingError, photo_color_frame, video_color_frame
from vacation_memories.media_scanner import MediaFile
from vacation_memories.ollama_client import OllamaAborted, OllamaClient, OllamaError
from vacation_memories.technical_analysis import Candidate
from vacation_memories.vision_schema import (
    PROMPT,
    RESPONSE_SCHEMA,
    VisionResponseError,
    VisionResult,
    prompt_fingerprint,
)

logger = logging.getLogger(__name__)

WARMUP_FRAMES = 3
ATTEMPTS = 2  # the first request plus at most one retry
MAX_CONSECUTIVE_FAILURES = 3  # frames in a row without an answer: the model is considered unavailable
NUM_PREDICT = 200  # the answer is a small JSON object
JPEG_QUALITY = 85
PROGRESS_LOG_SECONDS = 10.0


def prepare_image(image: Image.Image, resolution: int) -> str:
    """Base64 JPEG whose long side is at most `resolution` (smaller images are not enlarged)."""
    rgb = image.convert("RGB")
    rgb.thumbnail((resolution, resolution), Image.LANCZOS)
    buffer = io.BytesIO()
    rgb.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class VisionAnalyzer:
    """Asks the model about one image at a time, with a retry, and remembers the answers."""

    def __init__(
        self,
        client: OllamaClient,
        *,
        model: str,
        resolution: int,
        cache: CacheManager | None = None,
        unload_model: bool = True,
    ):
        self.client = client
        self.model = model
        self.resolution = resolution
        self.cache = cache
        self.unload_model = unload_model

    @property
    def params(self) -> str:
        """What changes the answers, and so belongs in the cache key."""
        return f"model={self.model}|prompt={prompt_fingerprint()}|resolution={self.resolution}"

    def analyze_image(self, image: Image.Image) -> VisionResult | None:
        """The validated answer, or None if the model did not give a usable one after the retry.

        Raises `OllamaAborted` when the job is stopping: that is not a failure of the model.
        """
        encoded = prepare_image(image, self.resolution)
        for attempt in range(1, ATTEMPTS + 1):
            try:
                content = self.client.chat(
                    prompt=PROMPT, image_b64=encoded, schema=RESPONSE_SCHEMA, num_predict=NUM_PREDICT
                )
                return VisionResult.from_response(json.loads(content))
            except OllamaAborted:
                raise
            except (OllamaError, VisionResponseError, ValueError) as error:
                logger.warning("vision request failed (attempt %d of %d): %s", attempt, ATTEMPTS, error)
        return None

    def lookup(self, media: MediaFile, timestamp: float | None) -> VisionResult | None:
        cached = self.cache.get_vision(media, timestamp, self.params) if self.cache else None
        return None if cached is None else VisionResult.from_response(cached)

    def store(self, media: MediaFile, timestamp: float | None, result: VisionResult) -> None:
        if self.cache:
            self.cache.put_vision(media, timestamp, self.params, result.to_dict())

    def close(self) -> None:
        """At the end of the job: free the model's memory (unless configured otherwise)."""
        if not self.unload_model or self.client.aborted:
            return
        try:
            self.client.unload()
        except OllamaError as error:
            logger.warning("cannot unload the model from Ollama: %s", error)

    def __enter__(self) -> "VisionAnalyzer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


@dataclass
class AiCoverage:
    """What the AI phase covered; recorded in the plan (`runtime.ai_coverage`)."""

    model: str
    mode: str  # "frames" (fixed number) or "time_budget"
    limit: float  # the number of frames, or the budget in seconds
    candidates: int  # offered to the phase
    selected: int = 0  # inside the limit of frames (all of them with a time budget)
    analyzed: int = 0  # with an answer, from the model or the cache
    from_cache: int = 0
    failed: int = 0
    skipped_by_budget: int = 0
    skipped_after_failures: int = 0
    aborted: bool = False
    seconds_per_frame: float | None = None
    placement: str = "unknown"  # where the model runs once it is loaded: gpu, cpu, mixed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AiOutcome:
    results: dict[str, VisionResult]  # candidate id -> answer; frames without one are absent
    coverage: AiCoverage


@dataclass(frozen=True)
class _Target:
    candidate_id: str
    media: MediaFile
    timestamp: float | None  # middle of the segment for a clip, None for a photo


@dataclass(frozen=True)
class _Done:
    target: _Target
    result: VisionResult | None = None
    failure: str | None = None  # "frame" (cannot be decoded) or "model" (no usable answer)
    seconds: float = 0.0
    skipped: bool = False
    stopped: bool = False  # the job is stopping


def prioritize(candidates: list[Candidate]) -> list[Candidate]:
    """Best non-semantic score first; equal scores keep their order."""
    return sorted(candidates, key=lambda candidate: -candidate.technical_score)


def analyze_candidates(
    candidates: list[Candidate],
    media_by_path: Mapping[Path, MediaFile],
    analyzer: VisionAnalyzer,
    *,
    frames: int | None = None,
    time_budget: float | None = None,
    warmup_frames: int = WARMUP_FRAMES,
    max_concurrency: int = 1,
    probe_placement: Callable[[], str] | None = None,
    progress: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    stop: Callable[[], bool] | None = None,
) -> AiOutcome:
    """Analyze the candidates in priority order within `frames` or `time_budget` seconds.

    `media_by_path` maps each candidate's relative path to its file. `probe_placement` is called
    once after the warm-up, when the model is loaded, and returns where it runs. When `stop`
    returns true no further frame is sent (the job is stopping); the caller decides what follows.
    """
    if frames is not None and time_budget is not None:
        raise ValueError("frames and time_budget are mutually exclusive")
    if frames is None and time_budget is None:
        time_budget = DEFAULT_AI_TIME_BUDGET_SECONDS
    coverage = AiCoverage(
        model=analyzer.model,
        mode="frames" if frames is not None else "time_budget",
        limit=frames if frames is not None else time_budget,
        candidates=len(candidates),
    )
    ordered = prioritize(candidates)
    selected = ordered[:frames] if frames is not None else ordered
    coverage.selected = len(selected)
    run = _Run(analyzer, coverage, len(selected), progress, clock)

    to_compute: list[_Target] = []
    for candidate in selected:
        media = media_by_path.get(candidate.rel_path)
        if media is None:
            logger.warning("no file for the candidate %s, skipping it", candidate.id)
            run.count_failure()
            continue
        target = _Target(candidate.id, media, _timestamp_of(candidate))
        cached = analyzer.lookup(media, target.timestamp)
        if cached is None:
            to_compute.append(target)
        else:
            run.accept(target, cached, from_cache=True)

    if to_compute:
        logger.info("AI analysis: %d frame(s) to analyze, %d already in the cache", len(to_compute), coverage.from_cache)
        _compute(run, to_compute, warmup_frames, max_concurrency, probe_placement, stop or (lambda: False))
    logger.info("AI analysis done: %s", coverage.to_dict())
    return AiOutcome(run.results, coverage)


def _timestamp_of(candidate: Candidate) -> float | None:
    return None if candidate.start is None else (candidate.start + candidate.end) / 2


class _Run:
    """Counters and results of one AI phase."""

    def __init__(self, analyzer, coverage, total, progress, clock):
        self.analyzer = analyzer
        self.coverage = coverage
        self.total = total
        self.progress = progress
        self.clock = clock
        self.results: dict[str, VisionResult] = {}
        self.done = 0
        self.consecutive_failures = 0
        self.last_log = clock()

    def accept(self, target: _Target, result: VisionResult, *, from_cache: bool = False) -> None:
        self.results[target.candidate_id] = result
        self.coverage.analyzed += 1
        self.coverage.from_cache += from_cache
        self._advance(1)

    def count_failure(self) -> None:
        self.coverage.failed += 1
        self._advance(1)

    def record(self, done: _Done) -> None:
        """Fold in the outcome of a frame that was sent to the model; stop after repeated failures."""
        if done.stopped:
            self._advance(1)
        elif done.skipped:
            self.coverage.skipped_after_failures += 1
            self._advance(1)
        elif done.result is not None:
            self.analyzer.store(done.target.media, done.target.timestamp, done.result)
            self.consecutive_failures = 0
            self.accept(done.target, done.result)
        else:
            if done.failure == "model":
                self.consecutive_failures += 1
                if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self.coverage.aborted:
                    self.coverage.aborted = True
                    logger.warning(
                        "the model gave no answer for %d frames in a row: stopping the AI analysis, "
                        "the remaining frames keep only their non-semantic scores",
                        self.consecutive_failures,
                    )
            self.count_failure()

    def skip(self, count: int, *, after_failures: bool) -> None:
        if after_failures:
            self.coverage.skipped_after_failures += count
        else:
            self.coverage.skipped_by_budget += count
        self._advance(count)

    def _advance(self, count: int) -> None:
        self.done += count
        if self.progress:
            self.progress(self.done, self.total)
        now = self.clock()
        if now - self.last_log >= PROGRESS_LOG_SECONDS:
            self.last_log = now
            logger.info("AI analysis: %d/%d frames", self.done, self.total)


def _compute(
    run: _Run,
    targets: list[_Target],
    warmup_frames: int,
    max_concurrency: int,
    probe_placement: Callable[[], str] | None,
    stopping: Callable[[], bool],
) -> None:
    coverage = run.coverage
    stop = threading.Event()

    def work(target: _Target) -> _Done:
        if stopping():
            return _Done(target, stopped=True)
        if stop.is_set():
            return _Done(target, skipped=True)
        start = run.clock()
        try:
            image = _load_frame(target)
        except SamplingError as error:
            logger.warning("cannot read %s for the AI analysis: %s", target.media.rel_path, error)
            return _Done(target, failure="frame")
        try:
            result = run.analyzer.analyze_image(image)
        except OllamaAborted:
            return _Done(target, stopped=True)
        return _Done(target, result, None if result else "model", run.clock() - start)

    # Warm-up, one request at a time: it measures the speed of the machine and loads the model.
    timings: list[float] = []
    consumed = 0
    while consumed < len(targets) and len(timings) < warmup_frames and not coverage.aborted and not stopping():
        done = work(targets[consumed])
        consumed += 1
        run.record(done)
        if done.result is not None:
            timings.append(done.seconds)
    remaining = targets[consumed:]
    if coverage.aborted:
        run.skip(len(remaining), after_failures=True)
        return
    if not timings:
        return

    coverage.seconds_per_frame = statistics.median(timings)
    logger.info("AI analysis: %.2f s per frame after a warm-up of %d frame(s)", coverage.seconds_per_frame, len(timings))
    if probe_placement is not None:
        try:
            coverage.placement = probe_placement()
        except Exception as error:  # informative only
            logger.warning("cannot tell where the model runs: %s", error)

    if coverage.mode == "time_budget":
        affordable = max(0, int(coverage.limit / coverage.seconds_per_frame) - len(timings))
        if len(remaining) > affordable:
            run.skip(len(remaining) - affordable, after_failures=False)
            remaining = remaining[:affordable]

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        for future in [pool.submit(work, target) for target in remaining]:
            run.record(future.result())
            if coverage.aborted:
                stop.set()


def _load_frame(target: _Target) -> Image.Image:
    if target.timestamp is None:
        return photo_color_frame(target.media.path)
    return video_color_frame(target.media.path, target.timestamp)
