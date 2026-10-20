"""Rendering parameters in one place: transitions, fades, tolerances and encoder settings.

The values that a user may want to change come from the single configuration (`from_settings`);
the rest are constants with a name, so no number is buried in a filter string.
"""

from dataclasses import dataclass

from vacation_memories.config import (
    DEFAULT_DURATION_TOLERANCE,
    DEFAULT_FADE_IN_SECONDS,
    DEFAULT_FADE_OUT_SECONDS,
    Settings,
)
from vacation_memories.transitions import TransitionRules


@dataclass(frozen=True)
class RenderConfig:
    rules: TransitionRules = TransitionRules()
    fade_in_seconds: float = DEFAULT_FADE_IN_SECONDS  # from black at the start of the video and of the music
    fade_out_seconds: float = DEFAULT_FADE_OUT_SECONDS  # to black at the end, in sync with the music
    music_loop_crossfade_seconds: float = 2.0

    # Checks (section 8, "FFprobe verification")
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE  # final file against the planned duration
    sync_start_tolerance: float = 0.05  # start_time of video and audio
    sync_duration_tolerance: float = 0.1  # duration of audio and video
    max_timestamp_jump_frames: int = 2
    segment_frame_tolerance: int = 1  # segment duration against the expected one, in frames

    # Encoding
    segment_preset: str = "veryfast"
    segment_crf: int = 16
    final_preset: str = "fast"
    final_crf: int = 20
    audio_sample_rate: int = 48000
    audio_bitrate: str = "192k"
    video_timescale: int = 90000  # the same time base for every segment (xfade needs it)

    # Preview (section "Resolution and format")
    preview_short_side: int = 540
    preview_fps: float = 24.0
    preview_preset: str = "ultrafast"
    preview_crf: int = 28

    # Photos with a slight Ken Burns effect
    photo_zoom: float = 0.06  # total zoom over the clip (6%)
    photo_overscale: float = 2.0  # the photo is composed this much larger than the output before zoompan
    photo_max_canvas_long_side: int = 4096

    blur_divisor: int = 8  # the blurred background is computed at 1/8 of the output size

    @classmethod
    def from_settings(cls, settings: Settings) -> "RenderConfig":
        return cls(
            rules=TransitionRules(
                default_seconds=settings.transition_duration,
                max_fraction=settings.transition_max_fraction,
                min_seconds=settings.transition_min_duration,
            ),
            fade_in_seconds=settings.fade_in_seconds,
            fade_out_seconds=settings.fade_out_seconds,
            duration_tolerance=settings.duration_tolerance,
        )
