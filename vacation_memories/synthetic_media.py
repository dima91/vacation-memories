"""Synthetic photos and clips for tests and for trials on ./dev-media.

Every scene is identified by a pattern, a hue and an overlaid label drawn with
Pillow's built-in font, so nothing depends on system fonts. Frames are
rendered with Pillow/NumPy and encoded by FFmpeg from an image sequence.

Usage: python -m vacation_memories.synthetic_media OUTPUT_DIR [--seed N]
"""

import argparse
import colorsys
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PATTERN_COUNT = 6
EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
EXIF_ORIENTATION = 0x0112
EXIF_DATETIME = 0x0132
EXIF_IFD = 0x8769
EXIF_DATETIME_ORIGINAL = 0x9003


def _scene_color(scene_id: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    hue = (scene_id * 0.137 + seed * 0.071) % 1.0
    strong = np.array(colorsys.hsv_to_rgb(hue, 0.85, 0.95)) * 255
    weak = np.array(colorsys.hsv_to_rgb((hue + 0.5) % 1.0, 0.6, 0.35)) * 255
    return strong, weak


def _pattern_mask(pattern: int, width: int, height: int, phase: float) -> np.ndarray:
    """Return an array in [0, 1] of shape (height, width)."""
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    unit = max(8, min(width, height) // 8)
    shift = phase * unit
    if pattern == 0:  # horizontal gradient
        return ((x + shift) % width) / width
    if pattern == 1:  # checkerboard
        return (((x + shift) // unit + y // unit) % 2).astype(np.float32)
    if pattern == 2:  # vertical stripes
        return (((x + shift) // unit) % 2).astype(np.float32)
    if pattern == 3:  # concentric rings
        radius = np.hypot(x - width / 2, y - height / 2)
        return (((radius + shift) // unit) % 2).astype(np.float32)
    if pattern == 4:  # diagonal stripes
        return (((x + y + shift) // unit) % 2).astype(np.float32)
    return (((x + shift) // unit % 2) * ((y // unit) % 2)).astype(np.float32)  # grid of blocks


def render_scene_frame(
    scene_id: int,
    width: int,
    height: int,
    *,
    seed: int = 0,
    t: float = 0.0,
    blur: float = 0.0,
    brightness: float = 1.0,
) -> Image.Image:
    """Render one frame of a scene: pattern, hue and label identify `scene_id`."""
    strong, weak = _scene_color(scene_id, seed)
    mask = _pattern_mask(scene_id % PATTERN_COUNT, width, height, t * 2)[..., None]
    pixels = weak + (strong - weak) * mask
    image = Image.fromarray(pixels.astype(np.uint8), "RGB")

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(max(10, height // 6))
    label = f"S{scene_id:03d}"
    box = draw.textbbox((6, 6), label, font=font)
    draw.rectangle((box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4), fill=(0, 0, 0))
    draw.text((6, 6), label, font=font, fill=(255, 255, 255))

    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    if brightness != 1.0:
        image = Image.fromarray(np.clip(np.asarray(image) * brightness, 0, 255).astype(np.uint8), "RGB")
    return image


def make_photo(
    path: Path,
    scene_id: int,
    width: int,
    height: int,
    *,
    seed: int = 0,
    capture_datetime: datetime | None = None,
    exif_orientation: int | None = None,
    blur: float = 0.0,
    brightness: float = 1.0,
) -> None:
    """Write a JPEG, PNG or HEIC photo, with optional EXIF date and orientation."""
    path = Path(path)
    image = render_scene_frame(scene_id, width, height, seed=seed, blur=blur, brightness=brightness)

    exif = Image.Exif()
    if exif_orientation is not None:
        exif[EXIF_ORIENTATION] = exif_orientation
    if capture_datetime is not None:
        stamp = capture_datetime.strftime(EXIF_DATE_FORMAT)
        exif[EXIF_DATETIME] = stamp
        exif.get_ifd(EXIF_IFD)[EXIF_DATETIME_ORIGINAL] = stamp

    suffix = path.suffix.lower()
    if suffix in (".heic", ".heif"):
        import pillow_heif

        pillow_heif.register_heif_opener()
        image.save(path, format="HEIF", exif=exif.tobytes())
    elif suffix == ".png":
        image.save(path, format="PNG")
    else:
        image.save(path, format="JPEG", quality=92, exif=exif)


def make_video(
    path: Path,
    scenes: list[tuple[int, float]],
    width: int,
    height: int,
    fps: int = 10,
    *,
    seed: int = 0,
    creation_time: datetime | None = None,
    rotation: int = 0,
    audio: bool = False,
) -> None:
    """Encode a clip made of consecutive scenes `(scene_id, seconds)` with H.264.

    `rotation` only adds rotation metadata: the tag is stored with `-c copy` in a second pass,
    because FFmpeg 5.1 ignores it while encoding. The sign convention is FFmpeg's.
    """
    path = Path(path)
    if width % 2 or height % 2:
        raise ValueError("video dimensions must be even")
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be 0, 90, 180 or 270")

    with tempfile.TemporaryDirectory(prefix="synthetic-frames-") as tmp:
        index = 0
        for scene_id, seconds in scenes:
            for n in range(round(seconds * fps)):
                frame = render_scene_frame(scene_id, width, height, seed=seed, t=n / fps)
                frame.save(Path(tmp) / f"f{index:05d}.png", compress_level=1)
                index += 1
        total = index / fps

        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                   "-framerate", str(fps), "-i", str(Path(tmp) / "f%05d.png")]
        if audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={total}"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-shortest"]
        date_tags = []
        if creation_time is not None:
            date_tags = ["-metadata", f"creation_time={creation_time.strftime('%Y-%m-%dT%H:%M:%S')}"]
        command += date_tags
        target = path.with_name(f".{path.stem}.unrotated{path.suffix}") if rotation else path
        command.append(str(target))
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL, capture_output=True)

    if rotation:
        tag = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(target),
               "-c", "copy", *date_tags, "-metadata:s:v:0", f"rotate={rotation}", str(path)]
        try:
            subprocess.run(tag, check=True, stdin=subprocess.DEVNULL, capture_output=True)
        finally:
            target.unlink(missing_ok=True)


@dataclass(frozen=True)
class GeneratedFile:
    """What the generator knows about a file it wrote (the ground truth)."""

    rel_path: Path
    expected_kind: str | None  # "photo", "video", or None for files that must be skipped
    width: int | None = None  # displayed size, after orientation
    height: int | None = None
    duration: float | None = None
    capture_datetime: datetime | None = None  # as written in EXIF/container; None when mtime is used
    date_source: str | None = None  # "exif", "container" or "mtime"
    mtime: float | None = None


_UNRELATED_MTIME = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()


def populate(output_dir: Path, seed: int = 0) -> list[GeneratedFile]:
    """Create a small reference set of media in `output_dir` and return its ground truth."""
    root = Path(output_dir)
    rng = random.Random(seed)
    manifest: list[GeneratedFile] = []

    def place(rel: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def record(item: GeneratedFile) -> None:
        os.utime(root / item.rel_path, (item.mtime or _UNRELATED_MTIME,) * 2)
        manifest.append(item)

    def photo(rel, scene, w, h, shown, *, when=None, orientation=None, mtime=None):
        make_photo(place(rel), scene, w, h, seed=seed, capture_datetime=when, exif_orientation=orientation)
        record(GeneratedFile(Path(rel), "photo", *shown, None, when,
                             "exif" if when else "mtime", mtime))

    def video(rel, scenes, w, h, shown, *, when=None, rotation=0, audio=False, mtime=None):
        make_video(place(rel), scenes, w, h, 10, seed=seed, creation_time=when, rotation=rotation, audio=audio)
        record(GeneratedFile(Path(rel), "video", *shown, sum(s for _, s in scenes), when,
                             "container" if when else "mtime", mtime))

    day1 = "2023-07-14_beach"
    day2 = "2023-07-15_town"
    photo(f"{day1}/IMG_0001.jpg", 1, 320, 240, (320, 240), when=datetime(2023, 7, 14, 9, 30, 5))
    photo(f"{day1}/IMG_0002.jpg", 2, 320, 240, (240, 320), when=datetime(2023, 7, 14, 10, 15, 0), orientation=6)
    photo(f"{day1}/IMG_0003.png", 3, 320, 240, (320, 240),
          mtime=datetime(2023, 7, 14, 12, 0, tzinfo=timezone.utc).timestamp())
    photo(f"{day1}/IMG_0004.heic", 4, 320, 240, (320, 240), when=datetime(2023, 7, 14, 17, 45, 30))
    video(f"{day2}/VID_0001.mp4", [(5, 1.0), (6, 1.0), (7, 1.0)], 320, 180, (320, 180),
          when=datetime(2023, 7, 15, 18, 0, 0))
    video(f"{day2}/VID_0002.mov", [(8, 2.0)], 180, 320, (180, 320),
          when=datetime(2023, 7, 15, 19, 30, 0), audio=True)
    video(f"{day2}/VID_0003.mkv", [(9, 1.5)], 320, 180, (320, 180),
          mtime=datetime(2023, 7, 15, 20, 0, tzinfo=timezone.utc).timestamp())
    video(f"{day2}/VID_0004.mp4", [(10, 1.5)], 320, 180, (180, 320),
          when=datetime(2023, 7, 15, 21, 0, 0), rotation=90)

    for rel in ("misc/broken.jpg", "misc/broken.mp4"):
        place(rel).write_bytes(rng.randbytes(2048))
        record(GeneratedFile(Path(rel), None))
    place("misc/notes.txt").write_text("not a media file\n")
    record(GeneratedFile(Path("misc/notes.txt"), None))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate a folder with synthetic photos and clips.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    manifest = populate(args.output, seed=args.seed)
    print(f"wrote {len(manifest)} files to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
