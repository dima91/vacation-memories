"""Dimensions, duration, FPS, orientation and capture date of photos and clips.

Capture date rules (documented fallback chain):
1. EXIF `DateTimeOriginal`, `DateTimeDigitized`, then `DateTime` (photos).
2. Container tags `com.apple.quicktime.creationdate`, then `creation_time` (clips).
3. The file's `mtime`, converted with the assumed time zone (`assume_tz`, default: the system's).

Embedded dates are naive and kept exactly as written, so grouping by day never depends on the
time zone of the machine that runs the job. Only the `mtime` fallback uses a time zone.
"""

import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image

from vacation_memories.cache_manager import CacheManager
from vacation_memories.media_probe import ProbeError, is_variable_frame_rate, parse_video_stream, stream_rotation
from vacation_memories.media_scanner import MediaFile, heic_available
from vacation_memories.process import ToolRunner

logger = logging.getLogger(__name__)

FFPROBE_TIMEOUT_SECONDS = 60
EXIF_IFD = 0x8769
EXIF_ORIENTATION = 0x0112
EXIF_DATE_TAGS = ((EXIF_IFD, 0x9003), (EXIF_IFD, 0x9004), (None, 0x0132))  # original, digitized, generic
CONTAINER_DATE_TAGS = ("com.apple.quicktime.creationdate", "creation_time")
EARLIEST_PLAUSIBLE_YEAR = 1971  # containers without a clock write 1970-01-01


class MetadataError(Exception):
    """The file is corrupt, unreadable or not a usable photo or clip."""


@dataclass(frozen=True)
class MediaMetadata:
    """`width` and `height` are the displayed size, after orientation or rotation.

    `exif_orientation` (photos, 1-8) and `rotation` (clips, degrees from the display matrix, as
    reported by ffprobe) are kept for the renderer. `is_vfr` flags clips with a variable frame
    rate (typical of phones), which the renderer converts to a constant one. `assumed_tz` is set
    only when the date comes from `mtime`.
    """

    kind: str
    width: int
    height: int
    capture_datetime: datetime
    date_source: str  # "exif", "container" or "mtime"
    duration: float | None = None
    fps: float | None = None
    exif_orientation: int = 1
    rotation: int = 0
    is_vfr: bool = False
    assumed_tz: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["capture_datetime"] = self.capture_datetime.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MediaMetadata":
        return cls(**{**data, "capture_datetime": datetime.fromisoformat(data["capture_datetime"])})


def metadata_params(assume_tz: str | None) -> str:
    """Cache parameters that change the extraction result (`fields` changes with the stored fields)."""
    return f"assume_tz={assume_tz or ''};fields=2"


def extract_metadata(media: MediaFile, assume_tz: str | None = None) -> MediaMetadata:
    """Read the metadata of one file; raise `MetadataError` if it is unusable."""
    zone = _resolve_zone(assume_tz)
    if media.kind == "photo":
        return _extract_photo(media, assume_tz, zone)
    return _extract_video(media, assume_tz, zone)


def load_metadata(
    files: list[MediaFile],
    cache: CacheManager,
    assume_tz: str | None = None,
    *,
    check: Callable[[], None] | None = None,
    progress: Callable[[], None] | None = None,
) -> list[tuple[MediaFile, MediaMetadata]]:
    """Metadata of every readable file, using the cache. Unusable files are logged and skipped.

    `check` is called before every file and may raise to stop; `progress` after every file.
    """
    _resolve_zone(assume_tz)
    params = metadata_params(assume_tz)
    loaded = []
    for media in files:
        if check:
            check()
        if progress:
            progress()
        cached = cache.get_metadata(media, params)
        if cached is not None:
            loaded.append((media, MediaMetadata.from_dict(cached)))
            continue
        try:
            metadata = extract_metadata(media, assume_tz)
        except MetadataError as error:
            logger.warning("skipping %s: %s", media.rel_path, error)
            continue
        cache.put_metadata(media, params, metadata.to_dict())
        loaded.append((media, metadata))
    return loaded


def _resolve_zone(assume_tz: str | None) -> ZoneInfo | None:
    if not assume_tz:
        return None
    try:
        return ZoneInfo(assume_tz)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown time zone: {assume_tz}") from error


def _mtime_fallback(media: MediaFile, zone: ZoneInfo | None, assume_tz: str | None) -> tuple[datetime, str]:
    moment = datetime.fromtimestamp(media.mtime_ns / 1e9, zone) if zone else datetime.fromtimestamp(media.mtime_ns / 1e9).astimezone()
    label = assume_tz if zone else f"system:{moment.tzname()}"
    return moment.replace(tzinfo=None), label


def _plausible(moment: datetime) -> bool:
    return moment.year >= EARLIEST_PLAUSIBLE_YEAR


def _extract_photo(media: MediaFile, assume_tz: str | None, zone: ZoneInfo | None) -> MediaMetadata:
    heic_available()  # registers the HEIC decoder when present
    try:
        with Image.open(media.path) as image:
            width, height = image.size
            exif = image.getexif()
            orientation = _exif_orientation(exif)
            captured = _exif_date(exif)
    except Exception as error:  # Pillow raises many types on corrupt data
        raise MetadataError(f"unreadable photo: {error}") from error

    if orientation >= 5:
        width, height = height, width
    source, tz_label = "exif", None
    if captured is None:
        captured, tz_label = _mtime_fallback(media, zone, assume_tz)
        source = "mtime"
    return MediaMetadata("photo", width, height, captured, source, exif_orientation=orientation, assumed_tz=tz_label)


def _exif_orientation(exif) -> int:
    try:
        value = int(exif.get(EXIF_ORIENTATION, 1))
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 8 else 1


def _exif_date(exif) -> datetime | None:
    for ifd, tag in EXIF_DATE_TAGS:
        try:
            raw = (exif.get_ifd(ifd) if ifd else exif).get(tag)
        except Exception:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="ignore")
        if not isinstance(raw, str):
            continue
        try:
            moment = datetime.strptime(raw.strip("\x00 "), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
        if _plausible(moment):
            return moment
    return None


def _run_ffprobe(media: MediaFile) -> dict:
    command = ["ffprobe", "-hide_banner", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(media.path)]
    try:
        result = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=FFPROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise MetadataError("ffprobe timed out") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise MetadataError(f"ffprobe failed: {detail[-1] if detail else result.returncode}")
    try:
        return json.loads(result.stdout.decode("utf-8", errors="replace"))
    except ValueError as error:
        raise MetadataError("ffprobe returned invalid output") from error


def _extract_video(media: MediaFile, assume_tz: str | None, zone: ZoneInfo | None) -> MediaMetadata:
    info = _run_ffprobe(media)
    video = next(
        (s for s in info.get("streams", [])
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None or not video.get("width") or not video.get("height"):
        raise MetadataError("no video stream")

    duration = _first_float(info.get("format", {}).get("duration"), video.get("duration"))
    if duration is None:
        raise MetadataError("unknown duration")

    rotation, _ = stream_rotation(video)
    width, height = video["width"], video["height"]
    if rotation in (90, 270):
        width, height = height, width

    captured = _container_date(info, video)
    source, tz_label = "container", None
    if captured is None:
        captured, tz_label = _mtime_fallback(media, zone, assume_tz)
        source = "mtime"
    return MediaMetadata("video", width, height, captured, source, duration=duration,
                         fps=_fps(video), rotation=rotation, is_vfr=_is_vfr(media, video), assumed_tz=tz_label)


def _first_float(*values) -> float | None:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return None


def _fps(video: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(video.get(key, "0/0"))
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return float(rate)
    return None


def _is_vfr(media: MediaFile, video: dict) -> bool:
    try:
        return is_variable_frame_rate(media.path, parse_video_stream(video), ToolRunner())
    except ProbeError as error:
        logger.warning("%s: cannot tell whether the frame rate is variable: %s", media.rel_path, error)
        return False


def _container_date(info: dict, video: dict) -> datetime | None:
    tags = {k.lower(): v for source in (info.get("format", {}), video) for k, v in source.get("tags", {}).items()}
    for name in CONTAINER_DATE_TAGS:
        raw = tags.get(name)
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            continue
        if _plausible(moment):
            return moment
    return None
