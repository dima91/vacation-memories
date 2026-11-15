"""Thumbnails, waveforms and per-clip previews, cached in `<workdir>/ui/` and regenerable (section 6 of `UI-SPEC.md`).

Everything here reads the originals and writes only inside the project's workdir. The previews are not
jobs: short FFmpeg runs of the server itself, one at a time, that do not take the workdir lock and do
not write `status.json`. They use the same runner as the rest of the program (`-nostdin`, process group).
"""

import contextlib
import json
import logging
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from vacation_memories.analysis.media_scanner import heic_available
from vacation_memories.core.process import ToolError, ToolRunner
from vacation_memories.editor.errors import ApiError, not_found
from vacation_memories.editor.ids import opaque_id
from vacation_memories.planning.edit_plan import PHOTO, OutputSpec, PlanElement

logger = logging.getLogger(__name__)

THUMB_LONG_SIDE = 320
THUMB_QUALITY = 80
WAVEFORM_BUCKETS = 800
WAVEFORM_RATE = 8000
CLIP_SHORT_SIDE = 360
CLIP_FPS = 24
CLIP_PHOTO_LONG_SIDE = 1280
FFMPEG_TIMEOUT_SECONDS = 180.0
UI_DIR = "ui"
_clip_lock = threading.Lock()  # one per-clip preview at a time: the machine is shared with the jobs


def _folder(workdir: Path, name: str) -> Path:
    folder = Path(workdir) / UI_DIR / name
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _publish(temporary: Path, target: Path) -> Path:
    os.replace(temporary, target)
    return target


def _upright(source: Path, long_side: int) -> Image.Image:
    heic_available()  # registers the HEIC decoder when it is there
    try:
        with Image.open(source) as image:
            upright = ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise ApiError(422, "unreadable_media", f"cannot read the photo: {error}") from None
    upright.thumbnail((long_side, long_side))
    return upright


def thumbnail(workdir: Path, source: Path, kind: str, runner: ToolRunner, at: float | None = None) -> Path:
    """A JPEG of a photo, or of a frame of a video (at `at` seconds, by default near the start)."""
    if not source.is_file():
        raise not_found("media")
    key = opaque_id(os.fsdecode(source))
    seconds = 0.0 if at is None else max(0.0, round(at, 1))
    folder = _folder(workdir, "thumbs")
    target = folder / (f"{key}.jpg" if kind == PHOTO else f"{key}-{int(seconds * 10)}.jpg")
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    temporary = folder / f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        if kind == PHOTO:
            _upright(source, THUMB_LONG_SIDE).save(temporary, format="JPEG", quality=THUMB_QUALITY)
        else:
            scale = f"scale={THUMB_LONG_SIDE}:{THUMB_LONG_SIDE}:force_original_aspect_ratio=decrease"
            try:
                runner.ffmpeg(["-ss", f"{seconds:.1f}", "-i", str(source), "-frames:v", "1", "-vf", scale,
                               "-q:v", "4", "-f", "mjpeg", str(temporary)], timeout=FFMPEG_TIMEOUT_SECONDS)
            except ToolError as error:
                raise ApiError(422, "unreadable_media", "cannot read a frame of the video") from error
            if not temporary.is_file() or temporary.stat().st_size == 0:  # a time past the end of the video
                raise ApiError(422, "unreadable_media", "there is no frame at that time")
        return _publish(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def waveform(workdir: Path, track: Path, runner: ToolRunner, buckets: int = WAVEFORM_BUCKETS) -> Path:
    """The peaks (0 to 1) of a music track, `buckets` of them, and its length, as JSON."""
    if not track.is_file():
        raise not_found("track")
    folder = _folder(workdir, "waveforms")
    target = folder / f"{opaque_id(os.fsdecode(track))}.json"
    info = track.stat()
    signature = [info.st_size, info.st_mtime_ns, buckets]
    try:
        if json.loads(target.read_text(encoding="utf-8")).get("signature") == signature:
            return target
    except (OSError, ValueError):
        pass
    try:
        result = runner.ffmpeg(["-i", str(track), "-vn", "-ac", "1", "-ar", str(WAVEFORM_RATE), "-f", "s16le", "-"],
                               timeout=FFMPEG_TIMEOUT_SECONDS)
    except ToolError as error:
        raise ApiError(422, "unreadable_track", "cannot read the music track") from error
    samples = np.frombuffer(result.stdout[: len(result.stdout) // 2 * 2], dtype="<i2")
    if samples.size == 0:
        raise ApiError(422, "unreadable_track", "the music track has no audio")
    peaks = [round(float(np.abs(chunk).max()) / 32768, 3) for chunk in np.array_split(samples, min(buckets, samples.size))]
    temporary = folder / f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        temporary.write_text(json.dumps({"duration": round(samples.size / WAVEFORM_RATE, 3), "peaks": peaks,
                                        "signature": signature}), encoding="utf-8")
        return _publish(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def clip_size(output: OutputSpec) -> tuple[int, int]:
    """The frame of a per-clip preview: the plan's proportions, short side `CLIP_SHORT_SIDE`, even sides."""
    scale = CLIP_SHORT_SIDE / min(output.width, output.height)
    return round(output.width * scale / 2) * 2, round(output.height * scale / 2) * 2


def clip_preview(workdir: Path, input_dir: Path, element: PlanElement, output: OutputSpec,
                 runner: ToolRunner) -> Path:
    """A low-resolution video of one element (a photo for its duration, a moment of a video), cached."""
    source = Path(input_dir) / element.source
    if not source.is_file():
        raise not_found("media")
    width, height = clip_size(output)
    info = source.stat()
    signature = opaque_id(f"{element.source}|{element.start}|{element.end}|{element.duration}|{width}x{height}|"
                          f"{info.st_size}|{info.st_mtime_ns}")[:10]
    prefix = opaque_id(element.id)
    folder = _folder(workdir, "clips")
    target = folder / f"{prefix}-{signature}.mp4"
    if target.is_file():
        return target
    frame = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={CLIP_FPS},format=yuv420p")
    encode = ["-vf", frame, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-movflags", "+faststart"]
    temporary = folder / f".{prefix}-{signature}.{os.getpid()}.partial.mp4"
    prepared = folder / f".{prefix}-{signature}.{os.getpid()}.jpg"
    with _clip_lock:
        if target.is_file():  # made by the request that held the lock
            return target
        try:
            if element.kind == PHOTO:
                _upright(source, CLIP_PHOTO_LONG_SIDE).save(prepared, format="JPEG", quality=85)
                arguments = ["-loop", "1", "-framerate", str(CLIP_FPS), "-t", f"{element.duration:.3f}",
                             "-i", str(prepared), *encode]
            else:
                arguments = ["-ss", f"{element.start:.3f}", "-t", f"{element.end - element.start:.3f}",
                             "-i", str(source), *encode]
            try:
                runner.ffmpeg([*arguments, "-f", "mp4", str(temporary)], timeout=FFMPEG_TIMEOUT_SECONDS)
            except ToolError as error:
                logger.warning("cannot make the preview of a clip: %s", error)
                raise ApiError(422, "unreadable_media", "cannot make the preview of this clip") from None
            _publish(temporary, target)
        finally:
            for leftover in (temporary, prepared):
                with contextlib.suppress(FileNotFoundError):
                    leftover.unlink()
    for old in folder.glob(f"{prefix}-*.mp4"):  # the element was trimmed: its earlier previews are stale
        if old != target:
            with contextlib.suppress(FileNotFoundError):
                old.unlink()
    return target
