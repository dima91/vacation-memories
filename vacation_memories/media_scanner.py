"""Recursive discovery of the photos and clips in the input folder (read-only)."""

import functools
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
HEIC_EXTENSIONS = frozenset({".heic", ".heif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv"})


@dataclass(frozen=True)
class MediaFile:
    """A media file found in the input folder.

    `rel_path`, `size` and `mtime_ns` identify the file in the cache, so the cache stays
    valid when the media folder moves to another location.
    """

    path: Path
    rel_path: Path
    kind: str  # "photo" or "video"
    size: int
    mtime_ns: int


@functools.cache
def heic_available() -> bool:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        return False
    return True


def scan_media(input_dir: Path, *, heic: bool | None = None) -> list[MediaFile]:
    """List the supported media under `input_dir`, sorted by relative path.

    Unreadable directories and files are logged and skipped. HEIC files are included
    only when `heic` is true (default: when the `pillow-heif` decoder is usable).
    """
    root = Path(input_dir).absolute()
    if not root.exists():
        raise FileNotFoundError(f"input folder not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"input path is not a folder: {root}")
    if heic is None:
        heic = heic_available()

    photo_extensions = PHOTO_EXTENSIONS | HEIC_EXTENSIONS if heic else PHOTO_EXTENSIONS
    skipped_heic = 0
    found: list[MediaFile] = []

    def on_error(error: OSError) -> None:
        logger.warning("cannot read folder %s: %s", error.filename, error.strerror)

    for folder, _dirs, names in os.walk(root, onerror=on_error):
        for name in names:
            extension = os.path.splitext(name)[1].lower()
            if extension in photo_extensions:
                kind = "photo"
            elif extension in VIDEO_EXTENSIONS:
                kind = "video"
            elif extension in HEIC_EXTENSIONS:
                skipped_heic += 1
                continue
            else:
                continue
            path = Path(folder) / name
            try:
                info = path.stat()
            except OSError as error:
                logger.warning("cannot read %s: %s", path, error.strerror)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            found.append(MediaFile(path, path.relative_to(root), kind, info.st_size, info.st_mtime_ns))

    if skipped_heic:
        logger.warning("skipped %d HEIC file(s): the pillow-heif decoder is not available", skipped_heic)
    found.sort(key=lambda media: os.fsencode(media.rel_path))
    return found
