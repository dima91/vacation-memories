"""Rendering from an edit plan: normalized segments, assembly, music, verification, report.

`render_plan` is the whole of section 8 for one plan, with no analysis involved, so it also serves
`--render-only` and hand-edited plans. The steps:

1. every element becomes a normalized segment (`segments.py`), in parallel on `jobs` threads;
2. the transitions are laid out on the timeline by the function shared with the planner, with the
   limits applied to transitions written by hand, and the skipped clips taken out;
3. the segments are assembled in blocks (`assembly.py`) and the music is added;
4. the file is written as `<name>.partial.mp4`, checked with FFprobe and only then renamed.

Nothing is written outside the workdir except the output file. A failed check leaves the
`.partial.mp4` in place and the result says so (exit code 3); a signal stops every FFmpeg process
and raises `Interrupted` (exit code 128 + signal), with the finished segments kept for the next run.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from vacation_memories import exit_codes
from vacation_memories.assembly import Assembler, MusicError, music_track_duration, plan_music
from vacation_memories.atomic import atomic_write_json
from vacation_memories.doctor import REQUIRED_FFMPEG_ENCODERS, REQUIRED_FFMPEG_FILTERS
from vacation_memories.edit_plan import EditPlan, OutputSpec, PlanError
from vacation_memories.encoders import choose_assembly_encoder
from vacation_memories.process import Interrupted, ShutdownController, ToolRunner, parallel_map
from vacation_memories.render_checks import Check, check_final, failed
from vacation_memories.render_config import RenderConfig
from vacation_memories.render_filters import BlockItem, even
from vacation_memories.runtime_profile import DetectionEnv, FFmpegInfo, detect_ffmpeg
from vacation_memories.segments import REGENERATED, REUSED, SKIPPED, SegmentBuilder, SegmentResult
from vacation_memories.transitions import layout_timeline, split_blocks
from vacation_memories.workdir import StatusWriter

logger = logging.getLogger(__name__)

REPORT_NAME = "render_report.json"
PARTIAL_SUFFIX = ".partial"
PREVIEW_SUFFIX = "_preview"
LOG_INTERVAL_SECONDS = 10.0
STATUS_INTERVAL_SECONDS = 2.0
MIN_UPSCALED_SHARE = 0.3  # warn when more than this share of the clips is scaled up


class RenderError(Exception):
    """Rendering cannot proceed. `exit_code` is the process exit code to use."""

    def __init__(self, message: str, exit_code: int = exit_codes.ERROR):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class RenderOptions:
    """How to render: parallelism, encoder choice, preview and music (the plan says what)."""

    jobs: int = 1
    threads_per_job: int | None = None
    block_size: int = 20
    hwaccel: str = "none"
    preview: bool = False
    music: Path | None = None
    config: RenderConfig = field(default_factory=RenderConfig)

    @classmethod
    def from_profile(cls, profile, settings, *, preview: bool = False, music: Path | None = None) -> "RenderOptions":
        return cls(
            jobs=profile.jobs,
            threads_per_job=profile.threads_per_job,
            block_size=profile.block_size,
            hwaccel=profile.hwaccel,
            preview=preview,
            music=music,
            config=RenderConfig.from_settings(settings),
        )


@dataclass
class RenderResult:
    ok: bool
    output: Path  # the final file, or the `.partial.mp4` when a check failed
    report: dict

    @property
    def exit_code(self) -> int:
        return exit_codes.OK if self.ok else exit_codes.VERIFICATION_FAILED


class Progress:
    """Phase, items done and estimated remaining time, into `status.json` and the log."""

    def __init__(self, status: StatusWriter | None):
        self.status = status
        self._lock = threading.Lock()
        self._name = ""
        self._total = 0
        self._done = 0
        self._started = 0.0
        self._last_log = 0.0
        self._last_status = 0.0

    def phase(self, name: str, total: int) -> None:
        with self._lock:
            self._name, self._total, self._done = name, total, 0
            self._started = self._last_log = time.monotonic()
        logger.info("%s (%d)", name, total)
        self._publish(force=True)

    def advance(self, count: int = 1) -> None:
        with self._lock:
            self._done += count
            now = time.monotonic()
            due = now - self._last_log >= LOG_INTERVAL_SECONDS
            if due:
                self._last_log = now
        if due:
            logger.info("%s: %d/%d", self._name, self._done, self._total)
        self._publish(force=self._done >= self._total)

    def _publish(self, force: bool = False) -> None:
        """Write `status.json`, at most every few seconds (it is an atomic write with an fsync)."""
        if self.status is None:
            return
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_status < STATUS_INTERVAL_SECONDS:
                return
            self._last_status = now
            elapsed = now - self._started
            remaining = elapsed / self._done * (self._total - self._done) if self._done else None
            self.status.update(phase=self._name, done=self._done, total=self._total, eta_seconds=remaining)


def preview_output(spec: OutputSpec, config: RenderConfig) -> OutputSpec:
    """Preview: short side 540 px, 24 fps, whatever the plan says."""
    scale = config.preview_short_side / min(spec.width, spec.height)
    return OutputSpec(even(spec.width * scale), even(spec.height * scale), config.preview_fps)


def render_plan(
    plan: EditPlan,
    *,
    input_dir: Path,
    workdir: Path,
    output: Path,
    options: RenderOptions | None = None,
    ffmpeg: FFmpegInfo | None = None,
    controller: ShutdownController | None = None,
    status: StatusWriter | None = None,
    finish_status: bool = True,
) -> RenderResult:
    """Render `plan` into `output` (a relative path is resolved inside the workdir)."""
    options = options or RenderOptions()
    controller = controller or ShutdownController()
    workdir, input_dir = Path(workdir).absolute(), Path(input_dir).absolute()
    try:
        result = _render(plan, input_dir, workdir, Path(output), options, ffmpeg, controller, status)
    except Interrupted:
        if status and finish_status:
            status.finish("interrupted")
        raise
    except BaseException:
        if status and finish_status:
            status.finish("failed")
        raise
    if status and finish_status:
        status.finish("done" if result.ok else "failed")
    return result


def _render(plan, input_dir, workdir, output, options, ffmpeg, controller, status) -> RenderResult:
    config = options.config
    warnings: list[str] = []

    def warn(message: str) -> None:
        logger.warning(message)
        warnings.append(message)

    if not plan.elements:
        raise PlanError("the plan has no elements to render")
    final_path = _output_path(output, workdir, input_dir, options.preview)
    spec = preview_output(plan.output, config) if options.preview else plan.output
    size, fps = (spec.width, spec.height), spec.frame_rate
    ffmpeg = ffmpeg or detect_ffmpeg(DetectionEnv(), logger.warning)
    _require_ffmpeg(ffmpeg)

    runner = ToolRunner(controller)
    track = _music_track(options, runner)
    segments_dir, tmp_root = workdir / "segments", workdir / "tmp"
    for folder in (segments_dir, tmp_root):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="render-", dir=tmp_root))
    partial = final_path.with_name(final_path.stem + PARTIAL_SUFFIX + final_path.suffix)
    progress = Progress(status)
    started = time.monotonic()
    try:
        threads = options.threads_per_job
        builder = SegmentBuilder(
            runner=runner, config=config, input_dir=input_dir, segments_dir=segments_dir, tmp_dir=work,
            size=size, fps=fps, ffmpeg_version=ffmpeg.ffmpeg_version, ffmpeg_filters=ffmpeg.filters,
            threads=threads, preview=options.preview,
        )

        progress.phase("normalizing segments", len(plan.elements))

        def make(item: tuple[int, object]) -> SegmentResult:
            result = builder.build(*item)
            progress.advance()
            return result

        results = parallel_map(make, list(enumerate(plan.elements)), max(1, options.jobs), controller)
        usable = [(element, result) for element, result in zip(plan.elements, results) if result.usable]
        skipped = [result for result in results if not result.usable]
        for result in skipped:
            warnings.append(f"skipped {result.source}: {result.reason}")
        if not usable:
            raise RenderError("no segment could be produced: nothing to render")
        for result in results:
            warnings.extend(result.warnings)

        # Timeline of the surviving clips: the shared layout function, with the plan's transitions.
        elements = [element for element, _ in usable]
        frames = [result.frames for _, result in usable]
        requests = [element.transition_in for element in elements]
        layout = layout_timeline(frames, requests, fps, config.rules)
        blocks, forced = split_blocks(layout, options.block_size)
        if forced:
            layout = layout_timeline(frames, requests, fps, config.rules, forced)
        for placement in layout.adjusted():
            warn(f"{elements[placement.index].source}: {placement.note}")

        music = None if track is None else plan_music(options.music, track, layout.duration, config)
        _warn_upscaled(usable, size, warn)

        encoder, fallback = choose_assembly_encoder(
            options.hwaccel, ffmpeg.hw_encoders, runner, config, preview=options.preview
        )
        if fallback:
            warn(f"using {encoder.name}: {fallback}")
        assembler = Assembler(
            runner=runner, config=config, fps=fps, ffmpeg_version=ffmpeg.ffmpeg_version,
            threads=(threads or 0) * max(1, options.jobs) or None,
        )
        paths = [result.path for _, result in usable]
        items = [BlockItem(p.frames, p.dissolve_frames) for p in layout.placements]

        partial.unlink(missing_ok=True)
        try:
            if len(blocks) == 1:
                progress.phase("encoding the video", 1)
                assembler.render_final(paths, items, partial, encoder, music)
                progress.advance()
            else:
                block_files = _render_blocks(
                    assembler, blocks, paths, items, work, options, controller, progress
                )
                progress.phase("encoding the video", 1)
                whole = [BlockItem(_block_frames(items[b.start : b.stop]), 0) for b in blocks]
                assembler.render_final(block_files, whole, partial, encoder, music)
                progress.advance()

            progress.phase("verifying the file", 1)
            checks = check_final(
                partial, runner, config, size=size, fps=fps, planned_duration=layout.duration
            )
            progress.advance()
        except BaseException:
            partial.unlink(missing_ok=True)  # truncated by the failure or the signal
            raise

        ok = not failed(checks)
        if ok:
            os.replace(partial, final_path)
        else:
            for check in failed(checks):
                warn(f"check failed: {check.name}: {check.detail}")
        report = _report(
            plan, options, final_path if ok else partial, ok, spec, layout, results, usable, blocks, forced,
            checks, music, encoder.name, fallback, warnings, time.monotonic() - started, final_path,
        )
        atomic_write_json(workdir / REPORT_NAME, report)
        _log_outcome(report)
        return RenderResult(ok, final_path if ok else partial, report)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _block_frames(items: list[BlockItem]) -> int:
    return sum(item.frames - item.dissolve_frames for item in items)


def _render_blocks(assembler, blocks, paths, items, work, options, controller, progress) -> list[Path]:
    progress.phase("assembling blocks", len(blocks))
    block_files = [work / f"block-{number:04d}.mp4" for number in range(len(blocks))]

    def make(number: int) -> Path:
        span = blocks[number]
        block_items = list(items[span.start : span.stop])
        block_items[0] = BlockItem(block_items[0].frames, 0)  # the first clip of a block has no incoming dissolve
        assembler.render_block(list(paths[span.start : span.stop]), block_items, block_files[number], options.preview)
        progress.advance()
        return block_files[number]

    parallel_map(make, list(range(len(blocks))), max(1, options.jobs), controller)
    return block_files


def _music_track(options: RenderOptions, runner: ToolRunner) -> float | None:
    """Length of the music, read first so that an unusable file stops the render before any work."""
    if options.music is None:
        return None
    try:
        return music_track_duration(options.music, runner)
    except MusicError as error:
        raise RenderError(str(error), exit_codes.USAGE) from error


def _warn_upscaled(usable, size: tuple[int, int], warn) -> None:
    """More than 30% of the clips smaller than the output: they are scaled up, which shows."""
    upscaled = 0
    for _, result in usable:
        if result.source_size and min(size[0] / result.source_size[0], size[1] / result.source_size[1]) > 1:
            upscaled += 1
    if usable and upscaled / len(usable) > MIN_UPSCALED_SHARE:
        warn(f"{upscaled} of {len(usable)} selected clips have a resolution lower than the output and are scaled up")


def _require_ffmpeg(ffmpeg: FFmpegInfo) -> None:
    if ffmpeg.ffmpeg_version is None or ffmpeg.ffprobe_version is None:
        raise RenderError("ffmpeg and ffprobe are required", exit_codes.REQUIREMENTS)
    missing = [name for name in REQUIRED_FFMPEG_ENCODERS if name not in ffmpeg.encoders]
    missing += [f"filter {name}" for name in REQUIRED_FFMPEG_FILTERS if name not in ffmpeg.filters]
    if missing:
        raise RenderError("FFmpeg lacks: " + ", ".join(missing), exit_codes.REQUIREMENTS)


def _output_path(output: Path, workdir: Path, input_dir: Path, preview: bool) -> Path:
    path = output if output.is_absolute() else workdir / output
    path = Path(os.path.abspath(path))
    if path.suffix.lower() != ".mp4":
        raise PlanError(f"the output must be an .mp4 file, got {path.name}")
    if preview:
        path = path.with_name(path.stem + PREVIEW_SUFFIX + path.suffix)
    parent = path.parent.resolve()
    if parent == input_dir.resolve() or input_dir.resolve() in parent.parents:
        raise PlanError(f"the output {path} is inside the input folder: nothing is ever written there")
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise PlanError(f"the folder of the output does not exist or is not writable: {parent}")
    return path


def _report(
    plan, options, output, ok, spec, layout, results, usable, blocks, forced, checks, music, encoder, fallback,
    warnings, elapsed, final_path,
) -> dict:
    elements = [element for element, _ in usable]
    return {
        "ok": ok,
        "output": str(output),
        "preview": options.preview,
        "output_spec": {"width": spec.width, "height": spec.height, "fps": spec.fps},
        "planned_duration": round(layout.duration, 3),
        "plan_planned_duration": plan.planned_duration,
        "obtained_duration": _obtained(checks),
        "elapsed_seconds": round(elapsed, 1),
        "encoder": {"name": encoder, "requested_hwaccel": options.hwaccel, "fallback_reason": fallback},
        "segments": {
            "total": len(results),
            "reused": sum(1 for r in results if r.status == REUSED),
            "regenerated": [r.source for r in results if r.status == REGENERATED],
            "skipped": [{"source": r.source, "reason": r.reason} for r in results if r.status == SKIPPED],
            "vfr_converted": [r.source for r in results if r.vfr_converted],
            "hdr_converted": [r.source for r in results if r.hdr_converted],
        },
        "transitions": {
            "dissolves": sum(1 for p in layout.placements if p.dissolve_frames),
            "cuts": sum(1 for p in layout.placements[1:] if not p.dissolve_frames),
            "adjusted": [
                {
                    "source": elements[p.index].source,
                    "requested": {"type": p.requested.type, "duration": p.requested.duration},
                    "effective": {"type": p.effective_type, "duration": layout.seconds(p.dissolve_frames)},
                    "reason": p.note,
                }
                for p in layout.adjusted()
            ],
        },
        "blocks": [len(block) for block in blocks],
        "block_boundaries_forced": sorted(forced),
        "music": None
        if music is None
        else {"path": str(music.path), "track_duration": round(music.track_duration, 3), "mode": music.mode,
              "repeats": music.repeats},
        "checks": [check.to_dict() for check in checks],
        "warnings": warnings,
    }


def _obtained(checks: list[Check]) -> float | None:
    for check in checks:
        if check.name == "duration":
            try:
                return float(check.detail.split(" s,")[0])
            except ValueError:
                return None
    return None


def _log_outcome(report: dict) -> None:
    planned, obtained = report["planned_duration"], report["obtained_duration"]
    logger.info("rendering %s: planned %.1f s, obtained %s s", "done" if report["ok"] else "FAILED", planned, obtained)
