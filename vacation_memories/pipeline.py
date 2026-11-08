"""The pipeline and its execution modes: analysis, plan and rendering in one working folder.

- no mode: scanning, analysis (technical, duplicates, AI), selection, plan and rendering;
- `--analyze-only`: scanning and analysis, saved in the cache; no plan, no rendering;
- `--dry-run`: analysis (from the cache when possible) and plan, printed with estimates; no rendering;
- `--render-only`: rendering of the existing `edit_plan.json`, even a hand-edited one; no analysis;
- `--resume`: the interrupted run of this workdir, with the parameters saved in `run_config.json`.
  Parameters that change the result may be repeated but not changed (code 2); performance and log
  options may change. A run whose plan was saved goes straight to rendering, reusing the segments.

The analysis (`analysis`, shared with the evaluation) is cached (metadata, candidates, AI answers):
changing `--duration`, the structure or the output format only redoes selection, plan and rendering.
A run holds the workdir lock, keeps `status.json` up to date and stops in an orderly way on SIGINT,
SIGTERM and SIGHUP.
"""

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from vacation_memories import exit_codes
from vacation_memories.analysis import Analysis, AnalysisRequest, RequirementsError, run_analysis
from vacation_memories.atomic import atomic_write_json
from vacation_memories.config import ConfigError, Settings
from vacation_memories.doctor import MIN_FREE_BYTES, ffmpeg_problem
from vacation_memories.edit_plan import EditPlan, OutputSpec, PlanError, load_plan, save_plan
from vacation_memories.edit_planner import plan_edit
from vacation_memories.estimates import estimate_render
from vacation_memories.process import Interrupted, ShutdownController, ToolRunner
from vacation_memories.render_config import RenderConfig
from vacation_memories.renderer import (
    Progress,
    RenderError,
    RenderOptions,
    preview_output,
    render_plan,
    resolve_output,
)
from vacation_memories.runtime_profile import (
    MIB,
    DetectionEnv,
    RuntimeProfile,
    detect_profile,
    format_profile,
    write_runtime_profile,
)
from vacation_memories.video_format import (
    LONG_DURATION_SECONDS,
    PRESETS,
    SLOW_SOFTWARE_SHORT_SIDE,
    FormatError,
    FrameSize,
    even,
    frame_size,
    resize_keeping_short_side,
)
from vacation_memories.workdir import WorkdirInUseError, WorkdirLock

logger = logging.getLogger(__name__)

FULL, ANALYZE_ONLY, DRY_RUN, RENDER_ONLY = "full", "analyze_only", "dry_run", "render_only"
PLAN_NAME = "edit_plan.json"
RUN_CONFIG_NAME = "run_config.json"
RUN_CONFIG_VERSION = 2
DURATION_PRECISION = 0.05  # the final file must be within 5% of the requested duration


@dataclass(frozen=True)
class RunRequest:
    mode: str = FULL
    resume: bool = False
    preview: bool = False
    mode_given: bool = False  # a mode flag was on the command line


@dataclass(frozen=True)
class RunParams:
    """What changes the result of a run; saved in `run_config.json` and compared by `--resume`."""

    mode: str
    preview: bool
    input: str
    output: str
    music: str | None
    duration: float
    width: int | None  # None with --render-only without --resolution/--aspect: the plan decides
    height: int | None
    resolution: str | None  # as given: --render-only resizes the frame of the plan with them
    aspect: str | None
    structure: str
    no_ai: bool
    model: str
    ai_resolution: int
    ai_frames: int | None
    ai_time_budget: float | None
    assume_tz: str | None
    selection: dict
    rendering: dict


def run_pipeline(
    settings: Settings,
    request: RunRequest,
    *,
    detection_env: DetectionEnv | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Run one mode of the pipeline and return the exit code."""
    try:
        params, explicit = _params(settings, request)
    except (ConfigError, FormatError) as error:
        return exit_codes.usage_error(error)
    if params.duration > LONG_DURATION_SECONDS and params.mode != RENDER_ONLY:
        logger.warning("a %.0f-minute video takes a very long analysis and rendering time", params.duration / 60)

    lock = WorkdirLock(settings.workdir)
    try:
        lock.acquire()
    except WorkdirInUseError as error:
        print(f"error: {error}", file=sys.stderr)
        return exit_codes.WORKDIR_IN_USE
    state = "failed"
    try:
        with ShutdownController() as controller:
            run = _Run(settings, params, request, lock, controller, detection_env, out)
            try:
                if request.resume:
                    params, plan_ready = _resume(settings.workdir, params, explicit)
                    run.params = params
                else:
                    plan_ready = False
                _check_paths(settings, params)
                _save_run_config(settings.workdir, params, "running", plan_ready=plan_ready)
                code = run.execute(resume_with_plan=plan_ready)
                state = "done" if code == exit_codes.OK else "failed"
                return code
            except Interrupted as interruption:
                logger.warning("%s: stopping; run again with --resume to continue", interruption)
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
                logger.exception("the run failed")
                return exit_codes.ERROR
    finally:
        _finish(settings.workdir, lock, state)


def detect_runtime(settings: Settings, env: DetectionEnv, workdir: Path, *, rendering: bool) -> RuntimeProfile:
    """The runtime profile, saved in the workdir, once FFmpeg and the free space are checked.

    Ollama is not contacted here: only the AI phase does, and only when the cache lacks answers.
    Raises `RequirementsError` (exit code 4) when something is missing.
    """
    profile = detect_profile(replace(settings, no_ai=True), env)
    try:
        write_runtime_profile(profile, Path(workdir) / "runtime_profile.json")
    except OSError as error:
        logger.warning("cannot save runtime_profile.json: %s", error.strerror or error)
    for line in format_profile(profile):
        logger.info("profile: %s", line)
    if problem := ffmpeg_problem(profile.ffmpeg, rendering=rendering):
        raise RequirementsError(f"{problem} (see `doctor`)")
    for name, free in (("workdir", profile.workdir_free_bytes), ("tmp", profile.tmp_free_bytes)):
        if free is not None and free < MIN_FREE_BYTES:
            raise RequirementsError(f"less than {MIN_FREE_BYTES // MIB} MiB free in the {name}")
    return profile


def _finish(workdir: Path, lock: WorkdirLock, state: str) -> None:
    try:
        lock.status.finish(state)
        _update_run_config(workdir, state=state)
    except OSError as error:
        logger.warning("cannot record the final state: %s", error)
    finally:
        lock.release()


# --- parameters, paths and resume ------------------------------------------------------------------


def _params(settings: Settings, request: RunRequest) -> tuple[RunParams, set[str]]:
    if settings.input is None:
        raise ConfigError("--input is required (the folder with the photos and clips)")
    sources = settings.sources
    size_given = settings.resolution is not None or settings.aspect is not None
    width = height = None
    if request.mode != RENDER_ONLY or size_given:
        width, height = settings.output_size
    params = RunParams(
        mode=request.mode,
        preview=request.preview,
        input=os.fsdecode(Path(settings.input).absolute()),
        output=os.fsdecode(settings.output),
        music=None if settings.music is None else os.fsdecode(settings.music),
        duration=settings.duration,
        width=width,
        height=height,
        resolution=settings.resolution,
        aspect=settings.aspect,
        structure=settings.structure,
        no_ai=settings.no_ai,
        model=settings.model,
        ai_resolution=settings.ai_resolution,
        ai_frames=settings.ai_frames,
        ai_time_budget=settings.ai_time_budget,
        assume_tz=settings.assume_tz,
        selection=settings.selection.to_dict(),
        rendering={
            "transition_duration": settings.transition_duration,
            "transition_max_fraction": settings.transition_max_fraction,
            "transition_min_duration": settings.transition_min_duration,
            "fade_in_seconds": settings.fade_in_seconds,
            "fade_out_seconds": settings.fade_out_seconds,
        },
    )
    explicit = {key for key in ("input", "output", "music", "duration", "structure", "no_ai", "model",
                                "ai_resolution", "ai_frames", "ai_time_budget", "assume_tz")
                if sources.get(key, "default") != "default"}
    explicit |= {"selection", "rendering"}  # from the configuration file, which a resume normally shares
    if size_given:
        explicit |= {"width", "height"}
        if request.mode == RENDER_ONLY:  # the same size can come from different options, fitted differently
            explicit |= {"resolution", "aspect"}
    if request.mode_given:
        explicit.add("mode")
    if request.preview:
        explicit.add("preview")
    return params, explicit


def _check_paths(settings: Settings, params: RunParams) -> None:
    """Fail at startup, not after the analysis: input, output folder, music file."""
    input_dir = Path(params.input)
    if not input_dir.is_dir():
        raise ConfigError(f"the input folder does not exist or is not a folder: {input_dir}")
    if params.mode != ANALYZE_ONLY:
        try:
            resolve_output(Path(params.output), Path(settings.workdir).absolute(), input_dir, params.preview)
        except PlanError as error:
            raise ConfigError(str(error)) from None
    if params.music is not None and params.mode in (FULL, RENDER_ONLY) and not Path(params.music).is_file():
        raise ConfigError(f"the music file does not exist: {params.music}")


def _run_config_path(workdir: Path) -> Path:
    return Path(workdir) / RUN_CONFIG_NAME


def _save_run_config(workdir: Path, params: RunParams, state: str, *, plan_ready: bool) -> None:
    atomic_write_json(_run_config_path(workdir), {
        "version": RUN_CONFIG_VERSION,
        "params": asdict(params),
        "state": state,
        "plan_ready": plan_ready,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _update_run_config(workdir: Path, **changes: object) -> None:
    path = _run_config_path(workdir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data.update(changes, updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    atomic_write_json(path, data)


def _resume(workdir: Path, params: RunParams, explicit: set[str]) -> tuple[RunParams, bool]:
    """The saved parameters, after checking that the ones given now do not contradict them."""
    try:
        saved = json.loads(_run_config_path(workdir).read_text(encoding="utf-8"))
        saved_params = saved["params"]
        if saved.get("version") != RUN_CONFIG_VERSION or set(saved_params) != set(asdict(params)):
            raise ValueError("unknown format")
    except FileNotFoundError:
        raise ConfigError(f"nothing to resume: {RUN_CONFIG_NAME} not found in {workdir}") from None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ConfigError(f"cannot resume: {RUN_CONFIG_NAME} is not usable ({error})") from None

    current = asdict(params)
    different = sorted(key for key in explicit if current[key] != saved_params[key])
    if different:
        details = ", ".join(f"{key} (saved {saved_params[key]!r}, now {current[key]!r})" for key in different)
        raise ConfigError(f"--resume with parameters different from the interrupted run: {details}")
    logger.info("resuming the run saved in %s", _run_config_path(workdir))
    return RunParams(**saved_params), bool(saved.get("plan_ready"))


# --- the run -------------------------------------------------------------------------------------------


class _Run:
    def __init__(self, settings, params, request, lock, controller, detection_env, out):
        self.settings: Settings = settings
        self.params: RunParams = params
        self.request: RunRequest = request
        self.lock: WorkdirLock = lock
        self.controller: ShutdownController = controller
        self.env: DetectionEnv = detection_env or DetectionEnv()
        self.out = out
        self.workdir = Path(settings.workdir).absolute()
        self.progress = Progress(lock.status)
        self.profile: RuntimeProfile | None = None

    def execute(self, *, resume_with_plan: bool) -> int:
        params = self.params
        self.profile = detect_runtime(self.settings, self.env, self.workdir,
                                      rendering=params.mode in (FULL, RENDER_ONLY, DRY_RUN))
        if params.mode == RENDER_ONLY:
            return self._render(self._load_plan_for_render_only())
        if resume_with_plan and params.mode == FULL:
            logger.info("the plan of the interrupted run is ready: going straight to rendering")
            return self._render(load_plan(self.workdir / PLAN_NAME))

        analysis = self._analyze()
        if params.mode == ANALYZE_ONLY:
            self._print_analysis(analysis)
            return exit_codes.OK
        plan, report = self._plan(analysis)
        if params.mode == DRY_RUN:
            self._print_dry_run(plan, report)
            return exit_codes.OK
        if not plan.elements:
            print("error: no usable material: nothing to render", file=sys.stderr)
            return exit_codes.ERROR
        return self._render(plan, report)

    # --- analysis ---

    def _analyze(self) -> Analysis:
        params = self.params
        request = AnalysisRequest(
            input=Path(params.input), workdir=self.workdir, duration=params.duration, assume_tz=params.assume_tz,
            no_ai=params.no_ai, model=params.model, ai_resolution=params.ai_resolution, ai_frames=params.ai_frames,
            ai_time_budget=params.ai_time_budget,
        )
        return run_analysis(request, self.settings, controller=self.controller, progress=self.progress, env=self.env)

    def _print_analysis(self, analysis: Analysis) -> None:
        counts, coverage = analysis.counts, analysis.coverage
        self.out(f"Analysis saved in the cache of {self.workdir}")
        self.out(f"  files: {counts['files']} ({counts['readable_files']} readable), candidates: {counts['candidates']}")
        self.out(f"  below the technical threshold: {counts['below_technical_threshold']}, "
                 f"duplicates: {counts['duplicates']}, eligible: {counts['eligible']}")
        self.out("  " + _describe_ai(coverage))

    # --- plan ---

    def _output_spec(self) -> OutputSpec:
        return OutputSpec(self.params.width, self.params.height)

    def _plan(self, analysis: Analysis) -> tuple[EditPlan, dict]:
        params, settings = self.params, self.settings
        self.progress.phase("planning", 1)
        ranking = analysis.ranking
        rules = RenderConfig.from_settings(settings).rules
        result = plan_edit(ranking, target=params.duration, output=self._output_spec(), config=settings.selection,
                           rules=rules, structure=params.structure, assumed_tz=params.assume_tz)
        coverage = analysis.coverage
        report = {
            **result.report,
            "ai": {"used": ranking.ai_used, "coverage": None if coverage is None else coverage.to_dict()},
            "candidates": analysis.counts,
            "notes": ranking.notes,
        }
        plan = replace(result.plan, runtime=report)
        save_plan(plan, self.workdir / PLAN_NAME)
        _update_run_config(self.workdir, plan_ready=True)
        self.progress.advance()
        for note in ranking.notes:
            logger.info(note)
        for warning in report["warnings"]:
            logger.warning(warning)
        logger.info("plan saved: %d clip(s), planned %.1f s of %.1f s requested", len(plan.elements),
                    plan.planned_duration or 0.0, params.duration)
        return plan, report

    def _print_dry_run(self, plan: EditPlan, report: dict) -> None:
        out = self.out
        out("Runtime profile:")
        for line in format_profile(self.profile):
            out(f"  {line}")
        spec = plan.output
        out(f"Plan ({report['structure']} structure): {len(plan.elements)} clip(s), planned "
            f"{plan.planned_duration or 0:.1f} s of {plan.requested_duration:.1f} s requested, "
            f"output {spec.width}x{spec.height} at {spec.fps:g} fps")
        for section in report["sections"]:
            note = " (budget redistributed)" if section["redistributed"] else ""
            out(f"  {section['name']:<13} {section['planned']:7.1f} s  {section['clips']:3d} clip(s){note}")
        out("  " + _describe_ai(report["ai"]["coverage"], used=report["ai"]["used"]))
        estimate = self._estimate(spec, plan.planned_duration or 0.0)
        out(f"  estimate: {estimate.describe()}" if estimate else "  estimate: not available")
        for warning in report["warnings"] + report["notes"]:
            out(f"  note: {warning}")
        out(f"Plan saved to {self.workdir / PLAN_NAME}")

    def _estimate(self, spec: OutputSpec, seconds: float):
        config = RenderConfig.from_settings(self.settings)
        if self.params.preview:
            spec = preview_output(spec, config)
        return estimate_render(spec, seconds, self.profile.jobs, config, ToolRunner(self.controller),
                               preview=self.params.preview, tmp_dir=self.workdir / "tmp")

    # --- rendering ---

    def _load_plan_for_render_only(self) -> EditPlan:
        params, settings = self.params, self.settings
        plan = load_plan(self.workdir / PLAN_NAME)
        if settings.sources.get("duration", "default") != "default" and plan.planned_duration is not None \
                and abs(params.duration - plan.planned_duration) > 0.5:
            logger.warning("--render-only uses the duration of the plan (%.1f s): --duration %.1f is ignored",
                           plan.planned_duration, params.duration)
        size = self._render_only_size(plan)
        if size is not None:
            plan = replace(plan, output=replace(plan.output, width=size.width, height=size.height))
        return plan

    def _render_only_size(self, plan: EditPlan) -> FrameSize | None:
        # From the parameters, not the settings: a plain --resume has neither option on its command line.
        resolution, aspect = self.params.resolution, self.params.aspect
        width, height = plan.output.width, plan.output.height
        if resolution is None and aspect is None:
            return None
        if resolution is None:
            return resize_keeping_short_side(width, height, aspect)
        if resolution in PRESETS and aspect is None:  # the plan keeps its proportions
            short = PRESETS[resolution]
            scale = short / min(width, height)
            return frame_size(f"{even(width * scale)}x{even(height * scale)}", None)
        return frame_size(resolution, aspect)

    def _render(self, plan: EditPlan, report: dict | None = None) -> int:
        params, settings = self.params, self.settings
        spec = plan.output
        if min(spec.width, spec.height) > SLOW_SOFTWARE_SHORT_SIDE and not params.preview:
            estimate = self._estimate(spec, plan.planned_duration or 0.0)
            detail = f" ({estimate.describe()})" if estimate else ""
            logger.warning("above 1080p the software encoder is much slower%s; try --preview for trials", detail)
        options = RenderOptions.from_profile(
            self.profile, settings, preview=params.preview,
            music=None if params.music is None else Path(params.music),
        )
        result = render_plan(
            plan, input_dir=Path(params.input), workdir=self.workdir, output=Path(params.output), options=options,
            ffmpeg=self.profile.ffmpeg, controller=self.controller, status=self.lock.status, finish_status=False,
        )
        requested = plan.requested_duration
        obtained = result.report.get("obtained_duration")
        self.out(f"Video: {result.output}")
        self.out(f"Duration: requested {_seconds(requested)}, planned {_seconds(plan.planned_duration)}, "
                 f"obtained {_seconds(obtained)}")
        insufficient = bool(report and report.get("insufficient_material"))
        if requested and obtained is not None and abs(obtained - requested) > DURATION_PRECISION * requested:
            reason = "not enough material (see the log)" if insufficient else "check the plan"
            logger.warning("the video lasts %.1f s instead of %.1f s: %s", obtained, requested, reason)
        if not result.ok:
            print(f"error: the final file failed the checks, it is left as {result.output}", file=sys.stderr)
        return result.exit_code


def _describe_ai(coverage, *, used: bool | None = None) -> str:
    if coverage is None:
        return "AI: not used (--no-ai): the selection uses only non-semantic scores"
    data = coverage if isinstance(coverage, dict) else coverage.to_dict()
    state = "used" if (used if used is not None else data["analyzed"] > 0) else "not used"
    return (f"AI: {state}, {data['analyzed']} of {data['candidates']} eligible candidate(s) analyzed "
            f"({data['from_cache']} from the cache), model {data['model']}")


def _seconds(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f} s"
