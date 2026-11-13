"""`evaluate prepare`: a blind comparison of edits of the user's vacation, ready to review in a browser.

Everything goes in `<workdir>/eval/<run_id>/`, which is private (previews and thumbnails of the
user's media) and self-contained, so it can be copied to the computer it is watched on:

- `variants/<letter>_preview.mp4`: a low-resolution preview (as `--preview`) of every variant, all
  with the same `--duration`. `full` is the pipeline; `random` the baseline of the evaluation (the
  planner's sections, budgets and durations, clips drawn by a recorded seed from every readable
  candidate); optional `no-ai` (the same candidates scored without the vision model) and
  `technical-only` (scored by `technical_score` alone);
- `thumbs/`: a small picture of every clip and of every moment of the gallery of left-out
  candidates: the best by score, one per file, plus a sample for each reason of exclusion;
- `review.html`: the page, blind: the variants are letters in an order drawn at random;
- `prepare.json`: everything `report` needs, so the report never reads the media or calls Ollama;
- `mapping.json`: which letter is which variant, and the seeds; only `report` reads it, after the
  ratings.

It holds the workdir lock (code 5 when busy), keeps `status.json` up to date and stops in an
orderly way on a signal (code 128 + signal; the same run id can be prepared again, and the cache
and the segments make it quick). The analysis is the pipeline's, through the same cache; the AI
follows the settings, with `--require-ai` (code 4 when Ollama or the model is missing).
"""

import hashlib
import json
import logging
import random
import re
import secrets
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from vacation_memories import __version__
from vacation_memories.analysis.frame_sampler import SamplingError, photo_color_frame, video_color_frame
from vacation_memories.analysis.runner import Analysis, AnalysisRequest, RequirementsError, run_analysis
from vacation_memories.core import exit_codes
from vacation_memories.core.atomic import atomic_write_json, atomic_write_text
from vacation_memories.core.config import ConfigError, Settings
from vacation_memories.core.process import Interrupted, ShutdownController
from vacation_memories.core.runtime_profile import DetectionEnv
from vacation_memories.core.workdir import PRIVATE_MODE, WorkdirInUseError, WorkdirLock
from vacation_memories.evaluate.labels import Segment, eval_folder, load_labels
from vacation_memories.evaluate.page import PAGE_NAME, page_data, render_page
from vacation_memories.pipeline import detect_runtime
from vacation_memories.planning.edit_plan import OutputSpec
from vacation_memories.planning.edit_planner import VIDEO_END_MARGIN_SECONDS, PlanningResult, plan_edit, plan_random
from vacation_memories.planning.scene_ranker import BELOW_THRESHOLD, MOSTLY_BLACK, Ranking, capture_time
from vacation_memories.planning.video_format import FormatError
from vacation_memories.rendering.render_config import RenderConfig
from vacation_memories.rendering.renderer import Progress, RenderError, RenderOptions, render_plan

logger = logging.getLogger(__name__)

PREPARE_NAME = "prepare.json"
MAPPING_NAME = "mapping.json"
PREPARE_VERSION = 1
MAPPING_VERSION = 1
FULL, RANDOM, NO_AI, TECHNICAL_ONLY = "full", "random", "no-ai", "technical-only"
VARIANTS = (FULL, RANDOM, NO_AI, TECHNICAL_ONLY)
DEFAULT_VARIANTS = (FULL, RANDOM)
LETTERS = "ABCD"
DEFAULT_DURATION_SECONDS = 90.0
THUMB_LONG_SIDE = 240
THUMB_QUALITY = 85
GALLERY_BEST = 12  # the best left-out candidates by score, one per file
GALLERY_SAMPLE_PER_REASON = 6  # and a sample of the others for each reason
TECHNICAL, DUPLICATE, BUDGET, RANKING, TOO_SHORT = "technical_threshold", "duplicate", "budget", "ranking", "too_short"
EXCLUSION_REASONS = (TECHNICAL, DUPLICATE, BUDGET, RANKING, TOO_SHORT)
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SEED_LIMIT = 2**31
SIMILARITY_DIGITS = 4


@dataclass(frozen=True)
class PrepareRequest:
    duration: float = DEFAULT_DURATION_SECONDS
    variants: tuple[str, ...] = DEFAULT_VARIANTS
    seed: int | None = None  # of the random variant; drawn and recorded when not given
    run_id: str | None = None  # default: the UTC date and time

    def __post_init__(self) -> None:
        unknown = [name for name in self.variants if name not in VARIANTS]
        if unknown:
            raise ConfigError(f"unknown variant(s) {', '.join(unknown)}: choose among {', '.join(VARIANTS)}")
        if FULL not in self.variants or len(set(self.variants)) != len(self.variants):
            raise ConfigError(f"--variants must name {FULL} once, plus any of {', '.join(VARIANTS[1:])}")
        if self.seed is not None and not 0 <= self.seed < SEED_LIMIT:
            raise ConfigError(f"--seed must be between 0 and {SEED_LIMIT - 1}")
        if self.run_id is not None and not RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ConfigError("--run-id is up to 64 letters, digits, dots, dashes or underscores, not starting with "
                              f"a dot or a dash; got {self.run_id!r}")


def parse_variants(text: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in text.split(",") if name.strip())


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def config_hash(settings: Settings) -> str:
    """What decides the variants besides the material: selection, structure, rendering and AI."""
    data = {
        "selection": settings.selection.to_dict(),
        "structure": settings.structure,
        "rendering": [settings.transition_duration, settings.transition_max_fraction,
                      settings.transition_min_duration, settings.fade_in_seconds, settings.fade_out_seconds],
        "ai": None if settings.no_ai else [settings.model, settings.ai_resolution],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def run_prepare(
    settings: Settings,
    request: PrepareRequest,
    *,
    detection_env: DetectionEnv | None = None,
    out=print,
) -> int:
    """`evaluate prepare`: write the run folder and print where the page is; returns the exit code."""
    try:
        input_dir = _check(settings, request)
    except ConfigError as error:
        return exit_codes.usage_error(error)
    run_id = request.run_id or default_run_id()
    workdir = Path(settings.workdir).absolute()
    run_dir = eval_folder(workdir) / run_id
    if (run_dir / PREPARE_NAME).exists():
        return exit_codes.usage_error(ConfigError(
            f"the run {run_id} is already prepared in {run_dir}: choose another --run-id"))

    lock = WorkdirLock(workdir)
    try:
        lock.acquire()
    except WorkdirInUseError as error:
        print(f"error: {error}", file=sys.stderr)
        return exit_codes.WORKDIR_IN_USE
    state = "failed"
    try:
        with ShutdownController() as controller:
            try:
                prepared = _Preparation(settings, request, run_id, run_dir, input_dir, lock, controller,
                                        detection_env or DetectionEnv()).run()
            except Interrupted as interruption:
                logger.warning("%s: the preparation stops; run it again to start over", interruption)
                state = "interrupted"
                return interruption.exit_code
            except (ConfigError, FormatError) as error:
                return exit_codes.usage_error(error)
            except RequirementsError as error:
                print(f"error: {error}", file=sys.stderr)
                return exit_codes.REQUIREMENTS
            except RenderError as error:
                print(f"error: {error}", file=sys.stderr)
                return error.exit_code
            except Exception:
                logger.exception("the preparation failed")
                return exit_codes.ERROR
        for line in _summary(prepared, run_dir):
            out(line)
        state = "done"
        return exit_codes.OK
    finally:
        try:
            lock.status.finish(state)
        except OSError as error:
            logger.warning("cannot record the final state: %s", error)
        lock.release()


def _check(settings: Settings, request: PrepareRequest) -> Path:
    if settings.input is None:
        raise ConfigError("--input is required (the folder with the photos and clips)")
    input_dir = Path(settings.input).absolute()
    if not input_dir.is_dir():
        raise ConfigError(f"the input folder does not exist or is not a folder: {input_dir}")
    if settings.no_ai and NO_AI in request.variants:
        raise ConfigError(f"the {NO_AI} variant is the full one without the AI: leave out --no-ai to compare them")
    if settings.music is not None and not Path(settings.music).is_file():
        raise ConfigError(f"the music file does not exist: {settings.music}")
    return input_dir


class _Preparation:
    def __init__(self, settings, request, run_id, run_dir, input_dir, lock, controller, env):
        self.settings: Settings = settings
        self.request: PrepareRequest = request
        self.run_id: str = run_id
        self.run_dir: Path = run_dir
        self.input_dir: Path = input_dir
        self.lock: WorkdirLock = lock
        self.controller: ShutdownController = controller
        self.env: DetectionEnv = env
        self.workdir = Path(settings.workdir).absolute()
        self.progress = Progress(lock.status)

    def run(self) -> dict:
        settings, request = self.settings, self.request
        eval_dir = eval_folder(self.workdir)
        labels = load_labels(eval_dir)  # a broken labels.json fails now, not after the rendering
        profile = detect_runtime(settings, self.env, self.workdir, rendering=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)  # what an interrupted preparation of this run left
        for folder in (eval_dir, self.run_dir, self.run_dir / "variants", self.run_dir / "thumbs"):
            folder.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)

        analysis = run_analysis(
            AnalysisRequest(input=self.input_dir, workdir=self.workdir, duration=request.duration,
                            assume_tz=settings.assume_tz, no_ai=settings.no_ai, model=settings.model,
                            ai_resolution=settings.ai_resolution, ai_frames=settings.ai_frames,
                            ai_time_budget=settings.ai_time_budget),
            settings, controller=self.controller, progress=self.progress, env=self.env,
        )
        seeds = {
            "random": request.seed if request.seed is not None else secrets.randbelow(SEED_LIMIT),
            "letters": secrets.randbelow(SEED_LIMIT),
            "gallery": secrets.randbelow(SEED_LIMIT),
        }
        everything = analysis.ranking_of_everything(settings.selection)
        results = self._plan(analysis, everything, seeds["random"])
        if not results[FULL].plan.elements:
            raise RenderError("no usable material: nothing above the technical threshold that is not a duplicate")
        order = list(request.variants)
        random.Random(seeds["letters"]).shuffle(order)
        letters = {LETTERS[number]: name for number, name in enumerate(order)}

        catalog = _Catalog(analysis, everything)
        excluded = _gallery(analysis, results[FULL], everything, settings.selection, random.Random(seeds["gallery"]))
        for letter in sorted(letters):
            catalog.add_plan(results[letters[letter]])
        for candidate_id, _, _ in excluded:
            catalog.add_candidate(candidate_id)
        keys = catalog.keys()
        self._thumbnails(catalog, keys)

        variants = {}
        for letter in sorted(letters):
            result = results[letters[letter]]
            variants[letter] = self._variant(letter, result, catalog, keys, analysis)
        self._render(variants, results, letters, profile)

        coverage = analysis.coverage
        prepared = {
            "version": PREPARE_VERSION,
            "run_id": self.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "code_version": __version__,
            "config_hash": config_hash(settings),
            "duration": request.duration,
            "structure": settings.structure,
            "ai": {"used": analysis.ranking.ai_used, "coverage": None if coverage is None else coverage.to_dict()},
            "candidates": analysis.counts,
            "days": catalog.days,
            "material_by_day": {str(day): count for day, count in sorted(catalog.material_by_day().items())},
            "segments": {key: catalog.entry(segment, key) for segment, key in keys.items()},
            "variants": variants,
            "excluded": [
                {"key": keys[catalog.segment_of_candidate(candidate_id)], "reason": reason, "picked": picked,
                 "score": _score(catalog.items, candidate_id)}
                for candidate_id, reason, picked in excluded
            ],
        }
        atomic_write_json(self.run_dir / MAPPING_NAME, {
            "version": MAPPING_VERSION, "run_id": self.run_id, "letters": letters, "seeds": seeds,
        })
        atomic_write_text(self.run_dir / PAGE_NAME, render_page(page_data(prepared, labels)))
        atomic_write_json(self.run_dir / PREPARE_NAME, prepared)  # last: the mark of a whole preparation
        return prepared

    # --- variants ---

    def _plan(self, analysis: Analysis, everything: Ranking, seed: int) -> dict[str, PlanningResult]:
        settings, request = self.settings, self.request
        config = settings.selection
        common = {"target": request.duration, "output": OutputSpec(*settings.output_size), "config": config,
                  "rules": RenderConfig.from_settings(settings).rules, "assumed_tz": settings.assume_tz}
        self.progress.phase("planning the variants", len(request.variants))
        results = {}
        for name in request.variants:
            self.controller.check()
            if name == RANDOM:
                results[name] = plan_random(everything, seed=seed, **common)
            else:
                ranking = {FULL: analysis.ranking, NO_AI: analysis.ranking_without_ai(config),
                           TECHNICAL_ONLY: _technical_only(analysis.ranking)}[name]
                results[name] = plan_edit(ranking, structure=settings.structure, **common)
            self.progress.advance()
        return results

    def _variant(self, letter: str, result: PlanningResult, catalog: "_Catalog", keys: dict, analysis: Analysis) -> dict:
        nearest = _nearest_similarities(result.chosen_by, analysis)
        clips = []
        for element, candidate_id, similarity in zip(result.plan.elements, result.chosen_by, nearest):
            segment = catalog.segment_of_element(element)
            clips.append({
                "key": keys[segment], "start": element.timestamp, "duration": element.duration,
                "section": element.section, "score": element.score, "nearest_similarity": similarity,
            })
        report = result.report
        return {
            "video": f"variants/{letter}_preview.mp4",
            "structure": report["structure"],
            "requested_duration": report["requested_duration"],
            "planned_duration": result.plan.planned_duration,
            "obtained_duration": None,
            "skipped_segments": 0,
            "sections": [{"name": section["name"], "budget": section["budget"], "planned": section["planned"],
                          "clips": section["clips"]} for section in report["sections"]],
            "clips": clips,
        }

    def _render(self, variants: dict, results: dict, letters: dict, profile) -> None:
        options = RenderOptions.from_profile(profile, self.settings, preview=True, music=self.settings.music)
        for letter in sorted(variants):
            self.controller.check()
            logger.info("rendering the preview of version %s", letter)
            result = render_plan(
                results[letters[letter]].plan, input_dir=self.input_dir, workdir=self.workdir,
                output=self.run_dir / "variants" / f"{letter}.mp4", options=options, ffmpeg=profile.ffmpeg,
                controller=self.controller, status=self.lock.status, finish_status=False,
                report_path=self.run_dir / "variants" / f"{letter}_render.json",
            )
            if not result.ok:
                raise RenderError(f"the preview of version {letter} failed the checks: it is left as {result.output}",
                                  exit_codes.VERIFICATION_FAILED)
            skipped = len(result.report["segments"]["skipped"])
            if skipped:
                logger.warning("version %s: %d clip(s) could not be rendered, so the start times of the page may be "
                               "late after them", letter, skipped)
            variants[letter].update(obtained_duration=result.report.get("obtained_duration"), skipped_segments=skipped)

    # --- thumbnails ---

    def _thumbnails(self, catalog: "_Catalog", keys: dict) -> None:
        self.progress.phase("thumbnails", len(keys))
        for segment, key in keys.items():
            self.controller.check()
            path = self.input_dir / segment.source
            try:
                if segment.start is None:
                    image = photo_color_frame(path)
                else:
                    image = video_color_frame(path, (segment.start + segment.end) / 2)
                image.thumbnail((THUMB_LONG_SIDE, THUMB_LONG_SIDE))
                image.save(self.run_dir / "thumbs" / f"{key}.jpg", "JPEG", quality=THUMB_QUALITY)
                catalog.thumbs[segment] = f"thumbs/{key}.jpg"
            except (SamplingError, OSError) as error:
                logger.warning("no thumbnail for %s: %s", segment.id, error)
            self.progress.advance()


class _Catalog:
    """The segments that the page shows (clips of every variant and left-out candidates), with their facts."""

    def __init__(self, analysis: Analysis, everything: Ranking):
        self.analysis = analysis
        self.items = {item.id: item for item in everything.ranked}
        self.candidates = {candidate.id: candidate for candidate in analysis.candidates}
        captured = [capture_time(c, analysis.metadata[c.rel_path]).date() for c in analysis.candidates]
        self.first_day: date | None = min(captured, default=None)
        self.days = max((self._day(when) for when in captured), default=0)
        self.facts: dict[Segment, dict] = {}
        self.thumbs: dict[Segment, str] = {}

    def _day(self, when: date) -> int:
        return (when - self.first_day).days + 1

    def _moment_type(self, candidate_id: str) -> str | None:
        """The AI's moment type of the candidate, the same in every variant (so it tells nothing of the variant)."""
        result = self.analysis.ai_results.get(candidate_id)
        return result.moment_type if result else None

    def _add(self, segment: Segment, candidate_id: str, kind: str, when: date) -> None:
        self.facts.setdefault(segment, {"kind": kind, "day": self._day(when),
                                        "moment_type": self._moment_type(candidate_id)})

    @staticmethod
    def segment_of_element(element) -> Segment:
        if element.start is None:
            return Segment(element.source)
        return Segment(element.source, round(element.start, 3), round(element.end, 3))

    def segment_of_candidate(self, candidate_id: str) -> Segment:
        candidate = self.candidates[candidate_id]
        source = candidate.rel_path.as_posix()
        if candidate.start is None:
            return Segment(source)
        return Segment(source, round(candidate.start, 3), round(candidate.end, 3))

    def add_plan(self, result: PlanningResult) -> None:
        for element, candidate_id in zip(result.plan.elements, result.chosen_by):
            when = datetime.fromisoformat(element.captured_at).date()
            self._add(self.segment_of_element(element), candidate_id, element.kind, when)

    def add_candidate(self, candidate_id: str) -> None:
        candidate = self.candidates[candidate_id]
        when = capture_time(candidate, self.analysis.metadata[candidate.rel_path]).date()
        self._add(self.segment_of_candidate(candidate_id), candidate_id, candidate.kind, when)

    def keys(self) -> dict[Segment, str]:
        """Opaque keys in the order of the files, whatever the variant that shows them."""
        ordered = sorted(self.facts, key=lambda s: (s.source, -1.0 if s.start is None else s.start, s.end or 0.0))
        return {segment: f"s{number}" for number, segment in enumerate(ordered, start=1)}

    def entry(self, segment: Segment, key: str) -> dict:
        return {**segment.to_dict(), **self.facts[segment], "thumb": self.thumbs.get(segment)}

    def material_by_day(self) -> Counter:
        """Eligible candidates (above the threshold, not duplicates) of each day."""
        return Counter(self._day(item.captured_at.date()) for item in self.analysis.ranking.ranked)


def _technical_only(ranking: Ranking) -> Ranking:
    ranked = [replace(item, narrative_score=item.technical_score, final_score=item.technical_score)
              for item in ranking.ranked]
    return Ranking(ranked, ranking.similarity, ranking.ai_used, list(ranking.notes), list(ranking.discarded))


def _nearest_similarities(chosen: tuple[str, ...], analysis: Analysis) -> list[float | None]:
    """For every clip, the hash similarity of the most similar other clip of the same edit."""
    duplicates = analysis.duplicates_of_everything
    index = {candidate_id: number for number, candidate_id in enumerate(duplicates.ids)}
    positions = [index[candidate_id] for candidate_id in chosen]
    nearest = []
    for number, position in enumerate(positions):
        others = [float(duplicates.similarity[position, other]) for count, other in enumerate(positions)
                  if count != number]
        nearest.append(round(max(others), SIMILARITY_DIGITS) if others else None)
    return nearest


def _gallery(analysis: Analysis, full: PlanningResult, everything: Ranking, config, rng: random.Random):
    """The left-out candidates to show: (candidate id, reason, "best" or "sample"), in chronological order.

    The reason is relative to the full plan: below the technical threshold, near-duplicate, or
    eligible but left out. An eligible one is out for its section's `budget` when it scores at
    least as high as the lowest clip chosen from the same pool, for its `ranking` otherwise, and
    `too_short` when its segment is shorter than any clip.
    """
    discarded = {item.id: item.reason for item in analysis.ranking.discarded}
    ranked = {item.id: item for item in analysis.ranking.ranked}
    lowest: dict[str, float] = {}
    for element, candidate_id in zip(full.plan.elements, full.chosen_by):
        if element.section in set(full.pools.values()):
            lowest[element.section] = min(lowest.get(element.section, 1.0), ranked[candidate_id].final_score)
    reasons = {}
    for candidate in analysis.candidates:
        if candidate.id in full.shown:
            continue
        reason = discarded.get(candidate.id)
        if reason in (BELOW_THRESHOLD, MOSTLY_BLACK):
            reasons[candidate.id] = TECHNICAL
        elif reason == "duplicate":
            reasons[candidate.id] = DUPLICATE
        elif candidate.id in full.pools:
            score = ranked[candidate.id].final_score
            reasons[candidate.id] = BUDGET if score >= lowest.get(full.pools[candidate.id], 0.0) else RANKING
        elif candidate.start is not None and \
                candidate.end - candidate.start - VIDEO_END_MARGIN_SECONDS < config.min_clip_seconds:
            reasons[candidate.id] = TOO_SHORT
        else:  # chosen for the Opening or the Closing, then dropped by a rule
            reasons[candidate.id] = RANKING

    items = {item.id: item for item in everything.ranked}
    picked, sources = {}, set()
    for candidate_id in sorted(reasons, key=lambda cid: (-items[cid].final_score, cid)):
        if len(picked) >= GALLERY_BEST:
            break
        source = items[candidate_id].candidate.rel_path
        if source not in sources:
            picked[candidate_id] = "best"
            sources.add(source)
    for reason in EXCLUSION_REASONS:
        others = sorted(cid for cid, why in reasons.items() if why == reason and cid not in picked)
        for candidate_id in rng.sample(others, min(GALLERY_SAMPLE_PER_REASON, len(others))):
            picked[candidate_id] = "sample"
    return [(cid, reasons[cid], picked[cid]) for cid in sorted(picked, key=lambda cid: (items[cid].captured_at, cid))]


def _score(items: dict, candidate_id: str) -> dict:
    item = items[candidate_id]
    return {"final": round(item.final_score, 4), "technical_score": round(item.technical_score, 4),
            "narrative_score": round(item.narrative_score, 4), "components": item.components}


def _summary(prepared: dict, run_dir: Path) -> list[str]:
    letters = sorted(prepared["variants"])
    run_id = prepared["run_id"]
    return [
        f"Review ready: {run_dir / PAGE_NAME}",
        f"  versions {', '.join(letters)} of {prepared['duration']:g} s each, "
        f"{len(prepared['excluded'])} left-out moment(s) in the gallery",
        "  copy the folder to the computer you watch it on (without the JSON files, which tell the versions apart),",
        f"  e.g. rsync -a --exclude '*.json' <server>:<WORK_DIR>/eval/{run_id}/ ~/vacation-review/{run_id}/",
        f"  open review.html, export ratings.json, copy it to WORK_DIR/eval/{run_id}/ and run",
        f"  docker compose run --rm --no-deps app evaluate report --run-id {run_id}",
    ]
