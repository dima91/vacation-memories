"""Synthetic photos and clips for tests and for trials on ./dev-media.

Every scene is identified by a pattern, a hue and an overlaid label drawn with
Pillow's built-in font, so nothing depends on system fonts. Frames are
rendered with Pillow/NumPy and encoded by FFmpeg from an image sequence.

Two kinds of scene: the regular patterns (`render_scene_frame`), and mosaics of
random color blocks (`render_mosaic_frame`), sharp and all different from one
another for the perceptual hash, which make a whole synthetic vacation
(`populate_vacation`) with known capture dates. The evaluation vacation
(`populate_evaluation_vacation`) adds places, repetitions, defects and important
moments, described in its `ground_truth.json`.

Usage: python -m vacation_memories.synthetic_media OUTPUT_DIR [--seed N]
       python -m vacation_memories.synthetic_media OUTPUT_DIR --vacation [--photos N] [--videos N] [--days N]
       python -m vacation_memories.synthetic_media OUTPUT_DIR --evaluation [--days N] [--places-per-day N]
"""

import argparse
import colorsys
import itertools
import os
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from vacation_memories.evaluate.ground_truth import (
    BLACK,
    BLURRED,
    MOTION,
    NEAR_DUPLICATE,
    SIMILAR_SCENE,
    UNIQUE,
    GroundTruth,
    TruthFile,
    TruthScene,
)

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
    _draw_label(image, scene_id)

    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    if brightness != 1.0:
        image = Image.fromarray(np.clip(np.asarray(image) * brightness, 0, 255).astype(np.uint8), "RGB")
    return image


MOSAIC_COLUMNS = 8
MOSAIC_ROWS = 6
MOSAIC_PAN_SPEED = 0.25  # frame widths per second
PALETTE_HUE_SPREAD = 0.08  # the blocks of a palette stay this close to its hue


@dataclass(frozen=True)
class MosaicView:
    """The part of a scene's mosaic a shot shows: offsets as fractions of the frame, and a zoom."""

    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0


def _mosaic_blocks(scene_id: int, seed: int, hue: float | None) -> np.ndarray:
    rng = np.random.RandomState((seed * 1_000_003 + scene_id) % 2**32)
    shape = (MOSAIC_ROWS, MOSAIC_COLUMNS + 4)
    if hue is None:
        return rng.randint(0, 256, (*shape, 3)).astype(np.uint8)
    hues = (hue + rng.uniform(-PALETTE_HUE_SPREAD, PALETTE_HUE_SPREAD, shape)) % 1.0
    channels = np.vectorize(colorsys.hsv_to_rgb)(hues, rng.uniform(0.4, 1.0, shape), rng.uniform(0.15, 1.0, shape))
    return (np.stack(channels, axis=-1) * 255).astype(np.uint8)


def render_mosaic_frame(
    scene_id: int,
    width: int,
    height: int,
    *,
    seed: int = 0,
    t: float = 0.0,
    blur: float = 0.0,
    brightness: float = 1.0,
    speed: float = MOSAIC_PAN_SPEED,
    view: MosaicView | None = None,
    hue: float | None = None,
) -> Image.Image:
    """A mosaic of color blocks, panning by `speed` frame widths per second, with the scene label.

    `view` frames another part of the same mosaic (the same scene seen from elsewhere); `hue` draws
    the blocks around one hue, like the palette of a place, instead of from every color.
    """
    blocks = _mosaic_blocks(scene_id, seed, hue)
    view = view or MosaicView()
    y, x = np.mgrid[0:height, 0:width]
    shift = round(t * speed * width)
    columns = ((x / view.zoom + view.x * width + shift) * MOSAIC_COLUMNS // width).astype(int) % blocks.shape[1]
    rows = np.clip(((y / view.zoom + view.y * height) * MOSAIC_ROWS // height).astype(int), 0, MOSAIC_ROWS - 1)
    pixels = blocks[rows, columns]
    image = Image.fromarray(pixels, "RGB")
    _draw_label(image, scene_id)
    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    if brightness != 1.0:
        image = Image.fromarray(np.clip(np.asarray(image) * brightness, 0, 255).astype(np.uint8), "RGB")
    return image


def _draw_label(image: Image.Image, scene_id: int) -> None:
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(max(10, image.height // 6))
    label = f"S{scene_id:03d}"
    box = draw.textbbox((6, 6), label, font=font)
    draw.rectangle((box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4), fill=(0, 0, 0))
    draw.text((6, 6), label, font=font, fill=(255, 255, 255))


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
    mosaic: bool = False,
    view: MosaicView | None = None,
    hue: float | None = None,
) -> None:
    """Write a JPEG, PNG or HEIC photo of a scene, with optional EXIF date and orientation.

    `view` and `hue` apply to mosaics (see `render_mosaic_frame`).
    """
    if mosaic:
        image = render_mosaic_frame(scene_id, width, height, seed=seed, blur=blur, brightness=brightness,
                                    view=view, hue=hue)
    else:
        image = render_scene_frame(scene_id, width, height, seed=seed, blur=blur, brightness=brightness)
    save_photo(image, path, capture_datetime=capture_datetime, exif_orientation=exif_orientation)


def save_photo(
    image: Image.Image,
    path: Path,
    *,
    capture_datetime: datetime | None = None,
    exif_orientation: int | None = None,
    quality: int = 92,
) -> None:
    """Write `image` as a JPEG (with `quality`), PNG or HEIC photo, with optional EXIF date and orientation."""
    path = Path(path)
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
        image.save(path, format="JPEG", quality=quality, exif=exif)


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
    mosaic: bool = False,
    scene_options: list[dict] | None = None,
) -> None:
    """Encode a clip made of consecutive scenes `(scene_id, seconds)` with H.264.

    `scene_options` gives, scene by scene, more arguments of the renderer (e.g. `speed`, `hue` or
    `brightness` of a mosaic). `rotation` only adds rotation metadata: the tag is stored with
    `-c copy` in a second pass, because FFmpeg 5.1 ignores it while encoding. The sign convention is FFmpeg's.
    """
    path = Path(path)
    if width % 2 or height % 2:
        raise ValueError("video dimensions must be even")
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be 0, 90, 180 or 270")
    options = scene_options or [{}] * len(scenes)
    if len(options) != len(scenes):
        raise ValueError("one set of scene options per scene is needed")

    render = render_mosaic_frame if mosaic else render_scene_frame
    with tempfile.TemporaryDirectory(prefix="synthetic-frames-") as tmp:
        index = 0
        for (scene_id, seconds), extra in zip(scenes, options):
            for n in range(round(seconds * fps)):
                frame = render(scene_id, width, height, seed=seed, t=n / fps, **extra)
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


VACATION_START = datetime(2023, 7, 14, 8, 0, 0)
VACATION_DAY_HOURS = 12.0  # material is spread from 8:00 to 20:00


def populate_vacation(
    output_dir: Path,
    *,
    photos: int = 60,
    videos: int = 4,
    days: int = 3,
    day_weights: list[float] | None = None,
    seed: int = 0,
    size: tuple[int, int] = (320, 240),
    video_scenes: tuple[float, ...] = (4.0, 4.0),
    blurred: int = 0,
    first_scene: int = 100,
) -> list[GeneratedFile]:
    """A synthetic vacation: distinct mosaic photos and clips spread over `days` days with known dates.

    `day_weights` sets the share of the material of each day (e.g. `[8, 1, 1]` puts 80% on the first).
    Clips are made of consecutive scenes of `video_scenes` seconds. The last `blurred` photos are
    strongly blurred, so they fall below the technical threshold.
    """
    root = Path(output_dir)
    weights = day_weights or [1.0] * days
    if len(weights) != days:
        raise ValueError("one weight per day is needed")
    width, height = size
    manifest: list[GeneratedFile] = []
    scene = first_scene
    items = [("photo", n) for n in range(photos)] + [("video", n) for n in range(videos)]
    per_day = _split(len(items), weights)
    rng = random.Random(seed)
    rng.shuffle(items)
    position = 0
    for day, count in enumerate(per_day):
        folder = root / f"{(VACATION_START + timedelta(days=day)).date().isoformat()}_day{day + 1}"
        folder.mkdir(parents=True, exist_ok=True)
        for slot, (kind, number) in enumerate(sorted(items[position:position + count])):
            when = VACATION_START + timedelta(days=day, hours=VACATION_DAY_HOURS * (slot + 0.5) / count)
            when = when.replace(microsecond=0)
            if kind == "photo":
                rel = folder.relative_to(root) / f"IMG_{number:04d}.jpg"
                blur = 8.0 if number >= photos - blurred else 0.0
                make_photo(root / rel, scene, width, height, seed=seed, capture_datetime=when, blur=blur, mosaic=True)
                manifest.append(GeneratedFile(rel, "photo", width, height, None, when, "exif"))
                scene += 1
            else:
                rel = folder.relative_to(root) / f"VID_{number:04d}.mp4"
                scenes = [(scene + offset, seconds) for offset, seconds in enumerate(video_scenes)]
                make_video(root / rel, scenes, width, height, 10, seed=seed, creation_time=when, mosaic=True)
                manifest.append(GeneratedFile(rel, "video", width, height, sum(video_scenes), when, "container"))
                scene += len(video_scenes)
            os.utime(root / rel, (_UNRELATED_MTIME, _UNRELATED_MTIME))
        position += count
    return manifest


def _split(total: int, weights: list[float]) -> list[int]:
    """`total` items in proportion to `weights` (largest remainder), at least one per day when possible."""
    raw = [total * weight / sum(weights) for weight in weights]
    counts = [int(value) for value in raw]
    for index in sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)[: total - sum(counts)]:
        counts[index] += 1
    return counts


EVALUATION_VERSION = 1  # bump when the evaluation vacation changes: an older one is generated again
# Measured with the hash backend: pairs of these views of a scene stay below the near-duplicate threshold
# (at most 0.875 against 0.906 on 40 scenes), while unrelated mosaics are about 0.57 alike. With the default
# seed the hash groups exactly the bursts; another seed may group a pair of views, or miss the cut between
# two scenes of a clip, and the metrics then measure the analysis as it is.
REFRAMINGS = (MosaicView(), MosaicView(0.10, 0.08, 1.2), MosaicView(0.04, 0.09, 1.2))
NEAR_DUPLICATE_BRIGHTNESS = 0.92
NEAR_DUPLICATE_QUALITY = 40
NEAR_DUPLICATE_SCALE = 0.9
BURST_SECONDS = 2  # between the photos of a burst
SLOW_PAN = 0.08  # frame widths per second: an ordinary, calm shot
FAST_PAN = 0.5  # measured: clearly more dynamic than a calm shot, without false scene cuts (0.6 has some)
EVALUATION_SCENE_SECONDS = 4.0
BLUR_RADIUS = 8.0
BLACK_BRIGHTNESS = 0.03
GOLDEN_RATIO_CONJUGATE = 0.618  # hues this far apart: neighboring places never look alike


def populate_evaluation_vacation(
    output_dir: Path,
    *,
    seed: int = 0,
    days: int = 4,
    places_per_day: int = 2,
    size: tuple[int, int] = (320, 240),
    check: Callable[[], None] | None = None,
) -> GroundTruth:
    """A synthetic vacation with known truth for `evaluate synthetic`; writes its `ground_truth.json` too.

    Every place (a stretch of a day, with its own palette) holds two scenes photographed from three
    framings (`similar_scene` groups), a burst of three near-identical photos (`near_duplicate`
    group) and a clip of three calm scenes, the middle one a fast pan (important `motion`) in every
    other place. Every day has a photo of a scene seen nowhere else (important `unique`) and a
    blurred photo; the first day a black photo, and the last clip ends with a black scene. `check`
    is called before every file and may raise to stop. Returns the `GroundTruth`.
    """
    if days < 1 or places_per_day < 1:
        raise ValueError("at least one day and one place per day are needed")
    writer = _EvaluationWriter(Path(output_dir), seed, size, check)
    rng = random.Random(seed)
    places = [f"p{number + 1}" for number in range(days * places_per_day)]
    hours = VACATION_DAY_HOURS / places_per_day
    shots: list[tuple[datetime, int, str, float, dict]] = []  # (when, day, place, hue, shot)
    scenes = itertools.count(1000)

    for day in range(days):
        for slot in range(places_per_day):
            number = day * places_per_day + slot
            place, hue = places[number], (number * GOLDEN_RATIO_CONJUGATE) % 1.0
            found = []
            for _ in range(2):
                scene = next(scenes)
                found += [{"kind": "similar", "scene": scene, "view": view} for view in REFRAMINGS]
            found.append({"kind": "burst", "scene": next(scenes)})
            found.append({"kind": "video", "scenes": [next(scenes) for _ in range(3)], "fast": number % 2 == 0})
            if slot == day % places_per_day:
                found.append({"kind": "unique", "scene": next(scenes)})
            if slot == places_per_day - 1:
                found.append({"kind": "blurred", "scene": next(scenes)})
            if number == 0:
                found.append({"kind": "black", "scene": next(scenes)})
            rng.shuffle(found)
            for position, shot in enumerate(found):
                when = VACATION_START + timedelta(days=day, hours=hours * (slot + (position + 0.5) / len(found)))
                shots.append((when.replace(microsecond=0), day + 1, place, hue, shot))
    shots.sort(key=lambda entry: entry[0])
    last_video = max(index for index, entry in enumerate(shots) if entry[4]["kind"] == "video")
    shots[last_video][4]["black_scene"] = next(scenes)

    for when, day, place, hue, shot in shots:
        writer.write(when, day, place, hue, shot)
    params = {"version": EVALUATION_VERSION, "days": days, "places_per_day": places_per_day, "size": list(size)}
    truth = GroundTruth(seed, params, days, tuple(places), tuple(writer.files))
    truth.save(output_dir)
    return truth


class _EvaluationWriter:
    """Writes the shots of the evaluation vacation, in time order, and records their truth."""

    def __init__(self, root: Path, seed: int, size: tuple[int, int], check: Callable[[], None] | None):
        self.root, self.seed, self.size, self.check = root, seed, size, check
        self.files: list = []
        self.numbers = itertools.count(1)

    def write(self, when: datetime, day: int, place: str, hue: float, shot: dict) -> None:
        self.folder = self.root / f"{(VACATION_START + timedelta(days=day - 1)).date().isoformat()}_day{day}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.day, self.place, self.hue = day, place, hue
        kind, scene = shot["kind"], shot.get("scene")
        if kind == "similar":
            truth = TruthScene(scene, None, None, f"similar-{scene}", SIMILAR_SCENE)
            self._photo(self._mosaic(scene, view=shot["view"]), when, truth)
        elif kind == "burst":
            truth = TruthScene(scene, None, None, f"burst-{scene}", NEAR_DUPLICATE)
            base = self._mosaic(scene)
            width, height = self.size
            smaller = base.resize((round(width * NEAR_DUPLICATE_SCALE), round(height * NEAR_DUPLICATE_SCALE)))
            self._photo(base, when, truth)
            self._photo(self._mosaic(scene, brightness=NEAR_DUPLICATE_BRIGHTNESS),
                        when + timedelta(seconds=BURST_SECONDS), truth)
            self._photo(smaller, when + timedelta(seconds=2 * BURST_SECONDS), truth, quality=NEAR_DUPLICATE_QUALITY)
        elif kind == "unique":
            self._photo(self._mosaic(scene), when, TruthScene(scene, None, None, important=True, important_reason=UNIQUE))
        elif kind == "blurred":
            self._photo(self._mosaic(scene, blur=BLUR_RADIUS), when, TruthScene(scene, None, None, defect=BLURRED))
        elif kind == "black":
            self._photo(self._mosaic(scene, brightness=BLACK_BRIGHTNESS), when,
                        TruthScene(scene, None, None, defect=BLACK))
        else:
            self._video(when, shot)

    def _mosaic(self, scene: int, **options) -> Image.Image:
        width, height = self.size
        return render_mosaic_frame(scene, width, height, seed=self.seed, hue=self.hue, **options)

    def _photo(self, image: Image.Image, when: datetime, truth, *, quality: int = 92) -> None:
        if self.check:
            self.check()
        path = self.folder / f"IMG_{next(self.numbers):04d}.jpg"
        save_photo(image, path, capture_datetime=when, quality=quality)
        os.utime(path, (_UNRELATED_MTIME, _UNRELATED_MTIME))
        self.files.append(TruthFile(path.relative_to(self.root).as_posix(), "photo", self.day, self.place,
                                    when.isoformat(), (truth,)))

    def _video(self, when: datetime, shot: dict) -> None:
        if self.check:
            self.check()
        scene_ids = list(shot["scenes"])
        options = [{"speed": speed, "hue": self.hue}
                   for speed in (SLOW_PAN, FAST_PAN if shot["fast"] else SLOW_PAN, SLOW_PAN)]
        if "black_scene" in shot:
            scene_ids.append(shot["black_scene"])
            options.append({"speed": SLOW_PAN, "hue": self.hue, "brightness": BLACK_BRIGHTNESS})
        path = self.folder / f"VID_{next(self.numbers):04d}.mp4"
        width, height = self.size
        make_video(path, [(scene, EVALUATION_SCENE_SECONDS) for scene in scene_ids], width, height, 10,
                   seed=self.seed, creation_time=when, mosaic=True, scene_options=options)
        os.utime(path, (_UNRELATED_MTIME, _UNRELATED_MTIME))
        truths = []
        for position, (scene, extra) in enumerate(zip(scene_ids, options)):
            start = position * EVALUATION_SCENE_SECONDS
            fast = extra["speed"] == FAST_PAN
            truths.append(TruthScene(scene, start, start + EVALUATION_SCENE_SECONDS, important=fast,
                                     important_reason=MOTION if fast else None,
                                     defect=BLACK if "brightness" in extra else None))
        self.files.append(TruthFile(path.relative_to(self.root).as_posix(), "video", self.day, self.place,
                                    when.isoformat(), tuple(truths)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Populate a folder with synthetic photos and clips.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--vacation", action="store_true", help="a whole synthetic vacation of mosaic scenes")
    parser.add_argument("--photos", type=int, default=60)
    parser.add_argument("--videos", type=int, default=4)
    parser.add_argument("--evaluation", action="store_true",
                        help="the evaluation vacation, with its ground_truth.json")
    parser.add_argument("--days", type=int, help="days of the vacation (default 3, or 4 with --evaluation)")
    parser.add_argument("--places-per-day", type=int, default=2, help="with --evaluation (default 2)")
    args = parser.parse_args(argv)
    if args.evaluation:
        truth = populate_evaluation_vacation(args.output, seed=args.seed, days=args.days or 4,
                                             places_per_day=args.places_per_day)
        manifest = truth.files
    elif args.vacation:
        manifest = populate_vacation(args.output, photos=args.photos, videos=args.videos, days=args.days or 3,
                                     seed=args.seed)
    else:
        manifest = populate(args.output, seed=args.seed)
    print(f"wrote {len(manifest)} files to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
