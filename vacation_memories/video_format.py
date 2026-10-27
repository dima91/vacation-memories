"""`--duration`, `--resolution` and `--aspect`: parsing, validation and the resulting frame size.

A resolution preset names the short side of the frame and `--aspect` gives the other one; explicit
`WxH` dimensions carry their own ratio. Sizes are always rounded to even values (H.264 with yuv420p).
"""

import re
from dataclasses import dataclass
from fractions import Fraction

DEFAULT_DURATION_SECONDS = 300.0
MIN_DURATION_SECONDS = 15.0
LONG_DURATION_SECONDS = 20 * 60  # above this a warning, not an error
DEFAULT_ASPECT = "16:9"
DEFAULT_RESOLUTION = "1080p"
ASPECTS = ("16:9", "9:16", "1:1", "4:3")
PRESETS = {"480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "4k": 2160}
MIN_SHORT_SIDE = 320
MAX_SHORT_SIDE = 2160
MAX_LONG_SIDE = 3840  # the long side of 4k 16:9, the largest preset
SLOW_SOFTWARE_SHORT_SIDE = 1080  # above this, warn about the software encoding time
ASPECT_TOLERANCE = 0.01  # relative; even rounding moves the ratio of explicit sizes slightly

DURATION_EXAMPLES = "300, 90s, 5m, 1m30s, 05:00, 1:05:00"
RESOLUTION_EXAMPLES = "480p, 720p, 1080p, 1440p, 4k (= 2160p) or WxH such as 1920x1080"

_SECONDS = re.compile(r"(\d+(?:\.\d+)?)s?")
_MINUTES = re.compile(r"(\d+)m(?:(\d+(?:\.\d+)?)s)?")
_CLOCK = re.compile(r"(?:(\d+):)?(\d+):(\d{2}(?:\.\d+)?)")
_SIZE = re.compile(r"(\d+)x(\d+)")


class FormatError(ValueError):
    """An invalid duration, resolution or aspect; the message lists the accepted formats."""


def parse_duration(value: object) -> float:
    """Seconds from `300`, `90s`, `5m`, `1m30s`, `mm:ss` or `h:mm:ss` (a TOML number counts as seconds)."""
    if isinstance(value, bool):
        raise FormatError(f"invalid duration {value!r}: accepted formats are {DURATION_EXAMPLES}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        seconds = _duration_text(value.strip().lower())
    else:
        raise FormatError(f"invalid duration {value!r}: accepted formats are {DURATION_EXAMPLES}")
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise FormatError(f"invalid duration {value!r}: accepted formats are {DURATION_EXAMPLES}")
    if seconds < MIN_DURATION_SECONDS:
        raise FormatError(f"the duration must be at least {MIN_DURATION_SECONDS:g} s, got {seconds:g} s")
    return seconds


def _duration_text(text: str) -> float:
    if match := _SECONDS.fullmatch(text):
        return float(match[1])
    if match := _MINUTES.fullmatch(text):
        seconds = float(match[2] or 0)
        if seconds >= 60:
            raise FormatError(f"invalid duration {text!r}: the seconds after the minutes must be below 60")
        return int(match[1]) * 60 + seconds
    if match := _CLOCK.fullmatch(text):
        hours, minutes, seconds = int(match[1] or 0), int(match[2]), float(match[3])
        if seconds >= 60 or (match[1] is not None and minutes >= 60):
            raise FormatError(f"invalid duration {text!r}: minutes and seconds must be below 60")
        return hours * 3600 + minutes * 60 + seconds
    raise FormatError(f"invalid duration {text!r}: accepted formats are {DURATION_EXAMPLES}")


def parse_aspect(value: object) -> str:
    text = value.strip() if isinstance(value, str) else value
    if text not in ASPECTS:
        raise FormatError(f"invalid aspect {value!r}: accepted values are {', '.join(ASPECTS)}")
    return text


def parse_resolution(value: object) -> str:
    """The normalized resolution text: a preset name (`4k` becomes `2160p`) or `WxH`."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text in PRESETS:
        return "2160p" if text == "4k" else text
    if _SIZE.fullmatch(text):
        return text
    raise FormatError(f"invalid resolution {value!r}: accepted formats are {RESOLUTION_EXAMPLES}")


@dataclass(frozen=True)
class FrameSize:
    width: int
    height: int

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def _ratio(aspect: str) -> Fraction:
    width, height = aspect.split(":")
    return Fraction(int(width), int(height))


def frame_size(resolution: str, aspect: str | None) -> FrameSize:
    """Width and height for a resolution (preset or `WxH`) and an aspect (None: default or from `WxH`)."""
    resolution = parse_resolution(resolution)
    if resolution in PRESETS:
        short = PRESETS[resolution]
        ratio = _ratio(parse_aspect(aspect or DEFAULT_ASPECT))
        size = FrameSize(_even(short * ratio), short) if ratio >= 1 else FrameSize(short, _even(short / ratio))
    else:
        width, height = (int(part) for part in resolution.split("x"))
        if aspect is not None:
            expected = float(_ratio(parse_aspect(aspect)))
            if height == 0 or abs(width / height - expected) / expected > ASPECT_TOLERANCE:
                raise FormatError(
                    f"the resolution {resolution} does not have the aspect {aspect}: "
                    f"pass only one of them, or matching values (e.g. 1920x1080 with 16:9)"
                )
        size = FrameSize(_even(width), _even(height))
    _check_limits(size, resolution)
    return size


def _check_limits(size: FrameSize, resolution: str) -> None:
    if not MIN_SHORT_SIDE <= size.short_side <= MAX_SHORT_SIDE:
        raise FormatError(
            f"invalid resolution {resolution}: the short side must be between {MIN_SHORT_SIDE} and "
            f"{MAX_SHORT_SIDE} px (accepted formats: {RESOLUTION_EXAMPLES})"
        )
    if max(size.width, size.height) > MAX_LONG_SIDE:
        raise FormatError(f"invalid resolution {resolution}: the long side must be at most {MAX_LONG_SIDE} px")


def resize_keeping_short_side(width: int, height: int, aspect: str) -> FrameSize:
    """New proportions for an existing frame: the short side stays, the aspect sets the other one."""
    short = min(width, height)
    ratio = _ratio(parse_aspect(aspect))
    size = FrameSize(_even(short * ratio), short) if ratio >= 1 else FrameSize(short, _even(short / ratio))
    _check_limits(size, f"{size.width}x{size.height}")
    return size

