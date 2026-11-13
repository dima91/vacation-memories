"""`evaluate synthetic`: the full plan against random plans, on a synthetic vacation with known truth.

The vacation is generated once for each seed and size, then reused, in
`<workdir>/synthetic-eval/<dataset>/media`: never in `eval/`, which holds the evaluations of real
material. It is analyzed without AI, with its own cache and lock in `.../work`, then planned by the
planner and by the random baseline over many seeds, at plan level only: nothing is rendered.
`synthetic_report.json` gives every metric of the full plan next to its distribution over the random
plans. It finds regressions and checks the infrastructure; it does not say whether a video is pleasant.
"""

import logging
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vacation_memories import __version__
from vacation_memories.analysis.runner import AnalysisRequest, run_analysis
from vacation_memories.core import exit_codes
from vacation_memories.core.atomic import atomic_write_json
from vacation_memories.core.config import ConfigError, Settings
from vacation_memories.core.process import Interrupted, ShutdownController
from vacation_memories.core.runtime_profile import DetectionEnv
from vacation_memories.core.workdir import PRIVATE_MODE, WorkdirInUseError, WorkdirLock
from vacation_memories.evaluate.ground_truth import GroundTruth, GroundTruthError, load_ground_truth
from vacation_memories.evaluate.metrics import compare, plan_metrics
from vacation_memories.evaluate.synthetic_media import EVALUATION_VERSION, populate_evaluation_vacation
from vacation_memories.planning.edit_plan import OutputSpec, save_plan
from vacation_memories.planning.edit_planner import plan_edit, plan_random
from vacation_memories.rendering.render_config import RenderConfig
from vacation_memories.rendering.renderer import Progress

logger = logging.getLogger(__name__)

SYNTHETIC_FOLDER = "synthetic-eval"
REPORT_NAME = "synthetic_report.json"
PLAN_NAME = "edit_plan.json"
REPORT_VERSION = 1
DEFAULT_DURATION_SECONDS = 90.0
DEFAULT_SEEDS = 100
DATASET_SIZE = (320, 240)
PLAN_OUTPUT = OutputSpec(1920, 1080)  # nothing is rendered: the frame size does not change the plan
NOTE = ("automatic metrics on synthetic data: they find regressions and check the infrastructure, "
        "they do not say whether the video is pleasant")


@dataclass(frozen=True)
class SyntheticRequest:
    duration: float = DEFAULT_DURATION_SECONDS
    seeds: int = DEFAULT_SEEDS
    dataset_seed: int = 0
    days: int = 4
    places_per_day: int = 2

    def __post_init__(self) -> None:
        if self.seeds < 1:
            raise ConfigError(f"--seeds must be at least 1, got {self.seeds}")
        if self.days < 1 or self.places_per_day < 1:
            raise ConfigError("the synthetic vacation needs at least one day and one place per day")

    @property
    def dataset_name(self) -> str:
        return f"seed{self.dataset_seed}-{self.days}d{self.places_per_day}p-v{EVALUATION_VERSION}"


def dataset_folder(workdir: Path, request: SyntheticRequest) -> Path:
    return Path(workdir) / SYNTHETIC_FOLDER / request.dataset_name


def run_synthetic(
    settings: Settings,
    request: SyntheticRequest,
    *,
    detection_env: DetectionEnv | None = None,
    out=print,
) -> int:
    """`evaluate synthetic`: print and save the comparison; returns the exit code."""
    folder = dataset_folder(settings.workdir, request)
    work = folder / "work"
    work.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    lock = WorkdirLock(work)
    try:
        lock.acquire()
    except WorkdirInUseError as error:
        print(f"error: {error}", file=sys.stderr)
        return exit_codes.WORKDIR_IN_USE
    state = "failed"
    try:
        with ShutdownController() as controller:
            try:
                report = evaluate_synthetic(settings, request, folder, controller=controller,
                                            progress=Progress(lock.status), env=detection_env or DetectionEnv())
            except Interrupted as interruption:
                logger.warning("%s: the synthetic evaluation stops", interruption)
                state = "interrupted"
                return interruption.exit_code
            except Exception:
                logger.exception("the synthetic evaluation failed")
                return exit_codes.ERROR
        atomic_write_json(folder / REPORT_NAME, report)
        for line in format_report(report):
            out(line)
        out(f"Report saved to {folder / REPORT_NAME}")
        state = "done"
        return exit_codes.OK
    finally:
        try:
            lock.status.finish(state)
        except OSError as error:
            logger.warning("cannot record the final state: %s", error)
        lock.release()


def evaluate_synthetic(
    settings: Settings,
    request: SyntheticRequest,
    folder: Path,
    *,
    controller: ShutdownController,
    progress: Progress,
    env: DetectionEnv,
) -> dict:
    """Generate or reuse the vacation in `folder`, analyze and plan it; returns the report."""
    media, work = folder / "media", folder / "work"
    truth = _dataset(media, request, controller, progress)
    analysis = run_analysis(
        AnalysisRequest(input=media, workdir=work, duration=request.duration, assume_tz=settings.assume_tz,
                        no_ai=True, model=settings.model, ai_resolution=settings.ai_resolution, ai_frames=None,
                        ai_time_budget=None),
        settings, controller=controller, progress=progress, env=env,
    )
    config = settings.selection
    rules = RenderConfig.from_settings(settings).rules
    progress.phase("planning", request.seeds + 1)
    full = plan_edit(analysis.ranking, target=request.duration, output=PLAN_OUTPUT, config=config, rules=rules)
    save_plan(full.plan, work / PLAN_NAME)
    full_metrics = plan_metrics(full.plan, full.report, truth, config)
    progress.advance()
    everything = analysis.ranking_of_everything(config)
    random_metrics = []
    for seed in range(request.seeds):
        controller.check()
        result = plan_random(everything, target=request.duration, output=PLAN_OUTPUT, config=config, rules=rules,
                             seed=seed)
        random_metrics.append(plan_metrics(result.plan, result.report, truth, config))
        progress.advance()
    return {
        "version": REPORT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_version": __version__,
        "dataset": {"folder": str(media), "seed": truth.seed, "params": truth.params, "files": len(truth.files)},
        "duration": request.duration,
        "random_seeds": request.seeds,
        "structure": full.report["structure"],
        "candidates": analysis.counts,
        "selection": config.to_dict(),
        "metrics": compare(full_metrics, random_metrics),
        "note": NOTE,
    }


def _dataset(media: Path, request: SyntheticRequest, controller: ShutdownController, progress: Progress) -> GroundTruth:
    """The vacation of `request`: the one already in `media`, or a new one generated in its place."""
    try:
        truth = load_ground_truth(media)
        if truth.seed == request.dataset_seed and truth.params == _params(request):
            return truth
    except GroundTruthError:
        pass
    # Generated aside and moved in place at the end, so an interrupted generation is never taken for a whole one.
    staging = media.with_name(media.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    progress.phase("generating the synthetic vacation", 0)
    truth = populate_evaluation_vacation(staging, seed=request.dataset_seed, days=request.days,
                                         places_per_day=request.places_per_day, size=DATASET_SIZE,
                                         check=controller.check)
    shutil.rmtree(media, ignore_errors=True)
    staging.rename(media)
    return truth


def _params(request: SyntheticRequest) -> dict:
    return {"version": EVALUATION_VERSION, "days": request.days, "places_per_day": request.places_per_day,
            "size": list(DATASET_SIZE)}


def format_report(report: dict) -> list[str]:
    dataset = report["dataset"]
    lines = [
        f"Synthetic evaluation: {report['duration']:g} s, {report['structure']} structure, "
        f"{report['random_seeds']} random plan(s), dataset seed {dataset['seed']} ({dataset['files']} files)",
        f"  {'metric':<26} {'full':>8} {'random mean ± std':>20} {'full better / tied':>18}",
    ]
    for name, entry in report["metrics"].items():
        spread = "-" if "random_mean" not in entry else f"{entry['random_mean']:.2f} ± {entry['random_std']:.2f}"
        better = "-" if "full_better_share" not in entry else (
            f"{entry['full_better_share']:.0%} / {entry['tie_share']:.0%}")
        lines.append(f"  {name:<26} {_number(entry['full']):>8} {spread:>20} {better:>18}")
    lines.append(f"  note: {report['note']}")
    return lines


def _number(value) -> str:
    if value is None:
        return "-"
    return f"{value:d}" if isinstance(value, int) else f"{value:.2f}"
