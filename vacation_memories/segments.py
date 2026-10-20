"""Phase A of rendering: every plan element becomes a normalized segment in `<workdir>/segments/`.

A segment is a frame-accurate cut (always re-encoded) with timestamps from zero, a constant frame
rate equal to `output.fps`, the output resolution (a blurred background for other proportions),
`yuv420p`, square pixels, no rotation metadata, the same time base as every other segment and no
audio. Segments are cached by source file, extent and output parameters, so a later render with the
same plan reuses them. Each one is validated with FFprobe; a non-conforming segment is regenerated
once with more conservative parameters, and skipped if it still does not conform.
"""

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps

from vacation_memories.atomic import atomic_write_json
from vacation_memories.edit_plan import PHOTO, PlanElement
from vacation_memories.encoders import fps_mode_args, software_encoder, thread_args
from vacation_memories.media_probe import Probe, ProbeError, is_variable_frame_rate, probe
from vacation_memories.media_scanner import heic_available
from vacation_memories.process import Interrupted, ToolError, ToolRunner
from vacation_memories.render_checks import check_segment, failed
from vacation_memories.render_config import RenderConfig
from vacation_memories.render_filters import (
    HDR_TO_SDR,
    Geometry,
    even,
    fps_text,
    photo_segment_graph,
    seconds_text,
    video_segment_graph,
)
from vacation_memories.transitions import seconds_to_frames

logger = logging.getLogger(__name__)

SEGMENT_FORMAT_VERSION = 1  # bump when the way segments are made changes: old files are not reused
GENERATED, REUSED, REGENERATED, SKIPPED = "generated", "reused", "regenerated", "skipped"


@dataclass
class SegmentResult:
    index: int
    source: str
    status: str
    path: Path | None = None
    frames: int = 0
    vfr_converted: bool = False
    hdr_converted: bool = False
    source_size: tuple[int, int] | None = None  # displayed size of the source, rotation applied
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None  # why it was skipped

    @property
    def usable(self) -> bool:
        return self.status != SKIPPED


class SegmentBuilder:
    def __init__(
        self,
        *,
        runner: ToolRunner,
        config: RenderConfig,
        input_dir: Path,
        segments_dir: Path,
        tmp_dir: Path,
        size: tuple[int, int],
        fps: Fraction,
        ffmpeg_version: str | None,
        ffmpeg_filters: frozenset[str],
        threads: int | None = None,
        preview: bool = False,
    ):
        self.runner = runner
        self.config = config
        self.input_dir = Path(input_dir)
        self.segments_dir = Path(segments_dir)
        self.tmp_dir = Path(tmp_dir)
        self.size = size
        self.fps = fps
        self.ffmpeg_version = ffmpeg_version
        self.ffmpeg_filters = ffmpeg_filters
        self.threads = threads
        self.preview = preview
        self._probes: dict[Path, Probe] = {}

    def frames_for(self, element: PlanElement) -> int:
        return max(1, seconds_to_frames(element.duration, self.fps))

    def build(self, index: int, element: PlanElement) -> SegmentResult:
        """Make (or reuse) the segment of one element. Raises only `Interrupted`."""
        source = self.input_dir / element.source
        frames = self.frames_for(element)
        result = SegmentResult(index, element.source, SKIPPED, frames=frames)
        try:
            stat = source.stat()
        except OSError as error:
            return self._skip(result, f"source not readable: {error.strerror or error}")
        path = self.segments_dir / f"{self._key(index, element, stat, frames)}.mp4"

        if path.exists() and not failed(self._check(path, frames)):
            result.status, result.path = REUSED, path
            self._read_notes(path, result)
            return result

        problem = ""
        for attempt, conservative in enumerate((False, True)):
            candidate = self.segments_dir / f".{path.stem}.{os.getpid()}.{index}.tmp.mp4"
            try:
                self._render(index, element, source, candidate, frames, conservative, result)
                bad = failed(self._check(candidate, frames))
                if not bad:
                    os.replace(candidate, path)
                    self._write_notes(path, result)
                    result.status, result.path = (REGENERATED if attempt else GENERATED), path
                    return result
                problem = "; ".join(f"{check.name}: {check.detail}" for check in bad)
            except Interrupted:
                raise
            except (ToolError, ProbeError, OSError, ValueError) as error:
                problem = str(error)
            finally:
                candidate.unlink(missing_ok=True)
            logger.warning(
                "segment of %s is not conforming (%s)%s",
                element.source, problem, "" if attempt else ": regenerating with conservative parameters",
            )
        return self._skip(result, problem)

    def _skip(self, result: SegmentResult, reason: str) -> SegmentResult:
        result.status, result.reason, result.path = SKIPPED, reason, None
        logger.warning("skipping %s: %s", result.source, reason)
        return result

    def _check(self, path: Path, frames: int):
        return check_segment(path, self.runner, self.config, size=self.size, fps=self.fps, frames=frames)

    @staticmethod
    def _notes_path(path: Path) -> Path:
        return path.with_suffix(".json")

    def _write_notes(self, path: Path, result: SegmentResult) -> None:
        """What was done to the source (VFR, HDR), kept for the report of later renders that reuse the file."""
        notes = {
            "vfr_converted": result.vfr_converted,
            "hdr_converted": result.hdr_converted,
            "source_size": result.source_size,
            "warnings": result.warnings,
        }
        atomic_write_json(self._notes_path(path), notes)

    def _read_notes(self, path: Path, result: SegmentResult) -> None:
        try:
            notes = json.loads(self._notes_path(path).read_text(encoding="utf-8"))
            result.vfr_converted = bool(notes.get("vfr_converted"))
            result.hdr_converted = bool(notes.get("hdr_converted"))
            size = notes.get("source_size")
            result.source_size = (int(size[0]), int(size[1])) if size else None
            result.warnings = [str(w) for w in notes.get("warnings", [])]
        except (OSError, ValueError, AttributeError):
            pass

    def _key(self, index: int, element: PlanElement, stat: os.stat_result, frames: int) -> str:
        identity = {
            "format": SEGMENT_FORMAT_VERSION,
            "source": os.fsencode(element.source).hex(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "kind": element.kind,
            "start": None if element.start is None else round(element.start, 6),
            "frames": frames,
            "output": [*self.size, str(self.fps)],
            "encoding": [self.config.segment_preset, self.config.segment_crf, self.config.video_timescale],
            "hdr_tonemap": bool(self._hdr_chain_available()),
            "zoom_in": index % 2 == 0 if element.kind == PHOTO else None,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]

    def _hdr_chain_available(self) -> str:
        return HDR_TO_SDR if {"zscale", "tonemap"} <= self.ffmpeg_filters else ""

    def _encoder_args(self, conservative: bool) -> list[str]:
        encoder = software_encoder(
            self.config.segment_preset, self.config.segment_crf, conservative=conservative,
            gop=round(float(self.fps)) if conservative else None,
        )
        return [
            *encoder.codec_args,
            *fps_mode_args(self.ffmpeg_version),
            "-r", fps_text(self.fps),
            "-video_track_timescale", str(self.config.video_timescale),
        ]

    def _render(
        self, index: int, element: PlanElement, source: Path, target: Path, frames: int, conservative: bool,
        result: SegmentResult,
    ) -> None:
        if element.kind == PHOTO:
            self._render_photo(index, element, source, target, frames, conservative, result)
        else:
            self._render_video(element, source, target, frames, conservative, result)

    def _probe_source(self, source: Path) -> Probe:
        if source not in self._probes:
            self._probes[source] = probe(source, self.runner)
        return self._probes[source]

    def _render_video(
        self, element: PlanElement, source: Path, target: Path, frames: int, conservative: bool,
        result: SegmentResult,
    ) -> None:
        info = self._probe_source(source)
        stream = info.video
        if stream is None:
            raise ProbeError("no video stream")
        result.source_size = stream.displayed_size
        if is_variable_frame_rate(source, stream, self.runner):
            result.vfr_converted = True

        hdr_filter = None
        if stream.is_hdr:
            hdr_filter = self._hdr_chain_available() or None
            if hdr_filter:
                result.hdr_converted = True
            else:
                message = "HDR clip converted without tone mapping (FFmpeg lacks zscale or tonemap): colors will look washed out"
                logger.warning("%s: %s", element.source, message)
                if message not in result.warnings:
                    result.warnings.append(message)

        graph = video_segment_graph(
            stream_index=stream.index,
            stored_size=(stream.width, stream.height),
            rotation=stream.rotation,
            sample_aspect_ratio=stream.sample_aspect_ratio,
            target=self.size,
            fps=self.fps,
            hdr_filter=hdr_filter,
            config=self.config,
        )
        start = seconds_text(element.start or 0.0)
        seek = ["-ss", start] if element.start else []
        before = ["-fflags", "+genpts"] if conservative else []
        # Accurate seeking: `-ss` before the input decodes from the previous keyframe and drops the
        # frames up to the requested one. After the input it decodes from the start (slower, safer).
        decode, filtering, encode = thread_args(self.threads)
        command = [
            *before, *([] if conservative else seek), *decode, "-i", str(source), *(seek if conservative else []),
            *filtering, "-filter_complex", graph, "-map", "[v]", "-an", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-frames:v", str(frames), *self._encoder_args(conservative), *encode, str(target),
        ]
        self.runner.ffmpeg(command)

    def _render_photo(
        self, index: int, element: PlanElement, source: Path, target: Path, frames: int, conservative: bool,
        result: SegmentResult,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="photo-", dir=self.tmp_dir) as folder:
            prepared = Path(folder) / "photo.png"
            photo_size, result.source_size = self._prepare_photo(source, prepared)
            graph = photo_segment_graph(
                photo_size=photo_size,
                target=self.size,
                fps=self.fps,
                frames=frames,
                zoom_in=index % 2 == 0,  # alternate zoom in and out: consecutive photos do not all drift the same way
                config=self.config,
            )
            decode, filtering, encode = thread_args(self.threads)
            command = [
                *decode, "-i", str(prepared), *filtering, "-filter_complex", graph, "-map", "[v]", "-an", "-sn", "-dn",
                "-map_metadata", "-1", "-frames:v", str(frames), *self._encoder_args(conservative), *encode, str(target),
            ]
            self.runner.ffmpeg(command)

    def _prepare_photo(self, source: Path, target: Path) -> tuple[tuple[int, int], tuple[int, int]]:
        """Decode with Pillow (HEIC included), apply the EXIF orientation, write an upright PNG.

        FFmpeg then needs neither HEIC support nor its own orientation handling. A photo larger
        than the oversized canvas is reduced here, with a good filter, before FFmpeg sees it.
        Returns the size of the written image and the original upright size.
        """
        heic_available()  # registers the HEIC decoder when present
        try:
            with Image.open(source) as image:
                upright = ImageOps.exif_transpose(image).convert("RGB")
        except Exception as error:  # Pillow raises many types on corrupt data
            raise ValueError(f"unreadable photo: {error}") from error

        out_w, out_h = self.size
        factor = max(1.0, min(self.config.photo_overscale, self.config.photo_max_canvas_long_side / max(out_w, out_h)))
        original = upright.size
        fit = Geometry(upright.size, (even(out_w * factor), even(out_h * factor))).foreground
        if upright.width > fit[0] or upright.height > fit[1]:
            upright = upright.resize(fit, Image.LANCZOS)
        upright.save(target, format="PNG", compress_level=1)
        return upright.size, original
