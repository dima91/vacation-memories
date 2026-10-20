"""FFprobe, parsed: the facts the renderer and its checks need about a file.

`probe` returns the first video stream and the first audio stream with exact rational frame rates
and time bases. `video_packet_times` lists the packet timestamps of a video stream without decoding
it, for the variable-frame-rate detection and for the cheap timestamp check of the final file.
"""

import json
import re
import statistics
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vacation_memories.process import ToolError, ToolRunner

HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
FRAME_RATE_TOLERANCE = 0.005  # relative difference between r_frame_rate and avg_frame_rate
VFR_INTERVAL_TOLERANCE = 0.2  # a frame interval this far (relative) from the median is irregular
VFR_IRREGULAR_FRACTION = 0.02  # share of irregular intervals that makes a clip variable-rate
PACKET_SCAN_SECONDS = 120


class ProbeError(Exception):
    """FFprobe could not read the file, or the file has no usable stream."""


@dataclass(frozen=True)
class VideoStream:
    index: int  # position among all the streams of the file
    width: int  # stored size, before any rotation
    height: int
    pix_fmt: str | None
    sample_aspect_ratio: Fraction
    r_frame_rate: Fraction
    avg_frame_rate: Fraction
    time_base: Fraction
    start_time: float | None
    duration: float | None
    rotation: int  # degrees to turn the stored picture to display it, from the display matrix or the tag
    has_rotation_metadata: bool
    color_transfer: str | None
    color_primaries: str | None
    color_space: str | None
    codec: str | None
    bit_depth: int | None

    @property
    def displayed_size(self) -> tuple[int, int]:
        """Size on screen: rotation applied and non-square pixels stretched."""
        width, height = (self.height, self.width) if self.rotation % 180 else (self.width, self.height)
        return round(width * self.sample_aspect_ratio), height

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    @property
    def is_high_bit_depth(self) -> bool:
        return bool(self.bit_depth and self.bit_depth > 8) or bool(
            self.pix_fmt and re.search(r"(9|10|12|14|16)(le|be)$", self.pix_fmt)
        )

    @property
    def frame_rate_is_irregular(self) -> bool:
        """The two rates FFprobe reports disagree: a hint of variable frame rate, not a proof."""
        return abs(self.r_frame_rate - self.avg_frame_rate) > self.r_frame_rate * FRAME_RATE_TOLERANCE


@dataclass(frozen=True)
class AudioStream:
    codec: str | None
    sample_rate: int
    channels: int
    start_time: float | None
    duration: float | None


@dataclass(frozen=True)
class Probe:
    format_duration: float | None
    format_start_time: float | None
    video: VideoStream | None
    audio: AudioStream | None


def probe(path: Path, runner: ToolRunner) -> Probe:
    """Read a file with FFprobe; raise `ProbeError` if it is unreadable."""
    try:
        result = runner.ffprobe(["-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=120)
        info = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except ToolError as error:
        raise ProbeError(str(error)) from error
    except ValueError as error:
        raise ProbeError("ffprobe returned invalid output") from error

    streams = info.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})
    return Probe(
        format_duration=_float(fmt.get("duration")),
        format_start_time=_float(fmt.get("start_time")),
        video=None if video is None else parse_video_stream(video),
        audio=None if audio is None else _audio(audio),
    )


def video_packet_times(path: Path, runner: ToolRunner, seconds: float | None = PACKET_SCAN_SECONDS) -> list[tuple[float | None, float | None]]:
    """`(pts, dts)` in seconds of the video packets, in decoding order. Nothing is decoded."""
    args = ["-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time", "-of", "csv=p=0"]
    if seconds is not None:
        args += ["-read_intervals", f"%+{seconds:g}"]
    try:
        result = runner.ffprobe([*args, str(path)], timeout=300)
    except ToolError as error:
        raise ProbeError(str(error)) from error
    times = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2:
            times.append((_float(parts[0]), _float(parts[1])))
    return times


def is_variable_frame_rate(path: Path, stream: VideoStream, runner: ToolRunner) -> bool:
    """Detect VFR clips (typical of phones).

    The rate FFprobe reports as `r_frame_rate` and the average one coincide for a constant rate, so
    when they do the clip is taken as constant. When they differ the packet timestamps decide: a
    clip is variable when enough of its frame intervals stray from the median one.
    """
    if not stream.frame_rate_is_irregular:
        return False
    presentation = sorted(pts for pts, _ in video_packet_times(path, runner) if pts is not None)
    if len(presentation) < 3:
        return True
    intervals = [b - a for a, b in zip(presentation, presentation[1:])]
    median = statistics.median(intervals)
    if median <= 0:
        return True
    irregular = sum(1 for gap in intervals if abs(gap - median) > VFR_INTERVAL_TOLERANCE * median)
    return irregular / len(intervals) > VFR_IRREGULAR_FRACTION


def stream_rotation(stream: dict) -> tuple[int, bool]:
    """Rotation of a video stream in degrees (0, 90, 180, 270) and whether any rotation metadata exists.

    The display matrix (`side_data_list`) wins over the legacy `rotate` tag.
    """
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            try:
                return round(float(side_data["rotation"])) % 360, True
            except (TypeError, ValueError):
                continue
    legacy = stream.get("tags", {}).get("rotate")
    if legacy is None:
        return 0, False
    try:
        return (-int(float(legacy))) % 360, True
    except ValueError:
        return 0, False


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _fraction(value: object, default: Fraction = Fraction(0)) -> Fraction:
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return default
    return parsed


def parse_video_stream(stream: dict) -> VideoStream:
    """A `streams` entry of FFprobe's JSON output."""
    if not stream.get("width") or not stream.get("height"):
        raise ProbeError("the video stream has no size")
    sar = _fraction(str(stream.get("sample_aspect_ratio", "1:1")).replace(":", "/"), Fraction(1))
    rotation, has_rotation = stream_rotation(stream)
    bits = stream.get("bits_per_raw_sample")
    return VideoStream(
        index=int(stream.get("index", 0)),
        width=int(stream["width"]),
        height=int(stream["height"]),
        pix_fmt=stream.get("pix_fmt"),
        sample_aspect_ratio=sar if sar > 0 else Fraction(1),
        r_frame_rate=_fraction(stream.get("r_frame_rate", "0/1")),
        avg_frame_rate=_fraction(stream.get("avg_frame_rate", "0/1")),
        time_base=_fraction(stream.get("time_base", "0/1")),
        start_time=_float(stream.get("start_time")),
        duration=_float(stream.get("duration")),
        rotation=rotation,
        has_rotation_metadata=has_rotation,
        color_transfer=stream.get("color_transfer"),
        color_primaries=stream.get("color_primaries"),
        color_space=stream.get("color_space"),
        codec=stream.get("codec_name"),
        bit_depth=int(bits) if str(bits).isdigit() else None,
    )


def _audio(stream: dict) -> AudioStream:
    return AudioStream(
        codec=stream.get("codec_name"),
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        start_time=_float(stream.get("start_time")),
        duration=_float(stream.get("duration")),
    )
