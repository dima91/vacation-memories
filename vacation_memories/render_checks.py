"""FFprobe verification of normalized segments and of the final file (section 8).

Each check returns a `Check` with a readable detail, so the render report records the outcome of
every one, not only the failures.
"""

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from vacation_memories.media_probe import Probe, ProbeError, probe, video_packet_times
from vacation_memories.process import ToolRunner
from vacation_memories.render_config import RenderConfig

RATE_MATCH_TOLERANCE = 1e-3  # relative difference between r_frame_rate and avg_frame_rate


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _check(name: str, ok: bool, detail: str) -> Check:
    return Check(name, bool(ok), detail)


def failed(checks: list[Check]) -> list[Check]:
    return [check for check in checks if not check.ok]


def check_segment(
    path: Path,
    runner: ToolRunner,
    config: RenderConfig,
    *,
    size: tuple[int, int],
    fps: Fraction,
    frames: int,
) -> list[Check]:
    """Resolution, SAR 1:1, yuv420p, constant frame rate, uniform time base, no rotation, duration."""
    try:
        info = probe(path, runner)
    except ProbeError as error:
        return [_check("readable", False, str(error))]
    video = info.video
    if video is None:
        return [_check("video-stream", False, "no video stream")]

    expected_duration = frames / fps
    duration = video.duration if video.duration is not None else info.format_duration
    tolerance = config.segment_frame_tolerance / fps
    rate_gap = abs(video.r_frame_rate - video.avg_frame_rate)
    return [
        _check("resolution", (video.width, video.height) == size, f"{video.width}x{video.height}, expected {size[0]}x{size[1]}"),
        _check("sar", video.sample_aspect_ratio == 1, f"sample aspect ratio {video.sample_aspect_ratio}"),
        _check("pixel-format", video.pix_fmt == "yuv420p", f"{video.pix_fmt}"),
        _check(
            "frame-rate",
            video.r_frame_rate == fps and rate_gap <= fps * RATE_MATCH_TOLERANCE,
            f"r_frame_rate {video.r_frame_rate}, avg_frame_rate {video.avg_frame_rate}, expected {fps}",
        ),
        _check(
            "time-base",
            video.time_base == Fraction(1, config.video_timescale),
            f"time_base {video.time_base}, expected 1/{config.video_timescale}",
        ),
        _check("rotation", not video.has_rotation_metadata, "rotation metadata present" if video.has_rotation_metadata else "none"),
        _check(
            "duration",
            duration is not None and abs(duration - float(expected_duration)) <= float(tolerance) + 1e-6,
            f"{duration if duration is None else round(duration, 3)} s, expected {float(expected_duration):.3f} s",
        ),
    ]


def check_final(
    path: Path,
    runner: ToolRunner,
    config: RenderConfig,
    *,
    size: tuple[int, int],
    fps: Fraction,
    planned_duration: float,
    expect_audio: bool = True,
) -> list[Check]:
    """The checks listed for the final file: duration, codecs, sync, timestamps."""
    try:
        info = probe(path, runner)
    except ProbeError as error:
        return [_check("readable", False, str(error))]
    checks = _stream_checks(info, config, size, fps, planned_duration, expect_audio)
    try:
        packets = video_packet_times(path, runner, seconds=None)
    except ProbeError as error:
        checks.append(_check("timestamps", False, str(error)))
    else:
        checks.append(_timestamps_check(packets, fps, config))
    return checks


def _stream_checks(info: Probe, config: RenderConfig, size, fps, planned: float, expect_audio: bool) -> list[Check]:
    video, audio = info.video, info.audio
    if video is None:
        return [_check("video-stream", False, "no video stream")]
    duration = info.format_duration
    checks = [
        _check(
            "duration",
            duration is not None and abs(duration - planned) <= config.duration_tolerance,
            f"{duration if duration is None else round(duration, 3)} s, planned {planned:.3f} s "
            f"(tolerance {config.duration_tolerance:g} s)",
        ),
        _check("video-codec", video.codec == "h264" and video.pix_fmt == "yuv420p", f"{video.codec} {video.pix_fmt}"),
        _check("resolution", (video.width, video.height) == size, f"{video.width}x{video.height}, expected {size[0]}x{size[1]}"),
        _check(
            "frame-rate",
            video.r_frame_rate == fps and abs(video.r_frame_rate - video.avg_frame_rate) <= fps * RATE_MATCH_TOLERANCE,
            f"r_frame_rate {video.r_frame_rate}, avg_frame_rate {video.avg_frame_rate}, expected {fps}",
        ),
    ]
    if not expect_audio:
        return checks
    if audio is None:
        checks.append(_check("audio-stream", False, "no audio stream"))
        return checks
    checks.append(
        _check(
            "audio-format",
            audio.codec == "aac" and audio.sample_rate == config.audio_sample_rate and audio.channels == 2,
            f"{audio.codec} {audio.sample_rate} Hz, {audio.channels} channel(s)",
        )
    )
    start_gap = _gap(video.start_time, audio.start_time)
    checks.append(
        _check(
            "sync-start",
            start_gap is not None and start_gap <= config.sync_start_tolerance,
            f"video starts at {video.start_time}, audio at {audio.start_time}",
        )
    )
    duration_gap = _gap(video.duration, audio.duration)
    checks.append(
        _check(
            "sync-duration",
            duration_gap is not None and duration_gap <= config.sync_duration_tolerance,
            f"video lasts {video.duration}, audio {audio.duration}",
        )
    )
    return checks


def _gap(left: float | None, right: float | None) -> float | None:
    return None if left is None or right is None else abs(left - right)


def _timestamps_check(packets: list[tuple[float | None, float | None]], fps: Fraction, config: RenderConfig) -> Check:
    """Cheap check on the packet timestamps: monotonic decoding times, no negative presentation
    times, no gap above `max_timestamp_jump_frames`. (B-frames legitimately give negative decoding
    times at the start, so only the presentation times must not be negative.)"""
    if not packets:
        return _check("timestamps", False, "no video packets")
    presentation = [pts for pts, _ in packets]
    if any(pts is None for pts in presentation):
        return _check("timestamps", False, "packets without a presentation timestamp")
    decoding = [dts for _, dts in packets if dts is not None]
    if any(b <= a for a, b in zip(decoding, decoding[1:])):
        return _check("timestamps", False, "decoding timestamps are not monotonic")
    if min(presentation) < -1e-6:
        return _check("timestamps", False, f"negative presentation timestamp {min(presentation):.3f}")
    ordered = sorted(presentation)
    largest = max((b - a for a, b in zip(ordered, ordered[1:])), default=0.0)
    limit = config.max_timestamp_jump_frames / float(fps)
    return _check(
        "timestamps",
        largest <= limit + 1e-6,
        f"{len(packets)} packets, largest gap {largest:.3f} s (limit {limit:.3f} s)",
    )
