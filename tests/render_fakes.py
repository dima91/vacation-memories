"""Synthetic sources and helpers for the rendering tests: solid-color clips, VFR and offset clips,
music, frame extraction and plan builders. Everything is generated with FFmpeg's own sources."""

import io
import subprocess
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

from vacation_memories.edit_plan import PHOTO, VIDEO, EditPlan, OutputSpec, PlanElement
from vacation_memories.media_probe import probe
from vacation_memories.process import ToolRunner
from vacation_memories.transitions import DISSOLVE, TransitionRequest

RED, GREEN, BLUE, YELLOW = "0xff0000", "0x00ff00", "0x0000ff", "0xffff00"
MAGENTA = (255, 0, 255)
RUNNER = ToolRunner()
# Like a phone clip: BT.709 limited range, tagged as such. (FFmpeg would otherwise encode generated
# RGB with the BT.601 matrix and no tag, and the renderer's BT.709 tags would shift the colors.)
BT709_FILTER = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
BT709_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *BT709_TAGS]


def ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True, stdin=subprocess.DEVNULL, capture_output=True,
    )


def solid_clip(path: Path, color: str, seconds: float, size=(320, 180), fps: int = 30, *, extra=(), codec_args=()) -> Path:
    """H.264 clip of one color."""
    ffmpeg(
        "-f", "lavfi", "-i", f"color=c={color}:s={size[0]}x{size[1]}:r={fps}:d={seconds}",
        "-vf", BT709_FILTER, *extra, *X264, *codec_args, str(path),
    )
    return path


def scenes_clip(path: Path, colors: list[str], seconds_each: float, size=(320, 180), fps: int = 30, *, extra=()) -> Path:
    """One clip made of consecutive solid-color scenes, to check which frames a cut picked."""
    inputs = []
    for color in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={color}:s={size[0]}x{size[1]}:r={fps}:d={seconds_each}"]
    graph = "".join(f"[{i}:v]" for i in range(len(colors))) + f"concat=n={len(colors)}:v=1:a=0,{BT709_FILTER}[v]"
    ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", *extra, *X264, str(path))
    return path


def vfr_clip(path: Path, seconds: float = 3.0, size=(320, 180)) -> Path:
    """A clip whose frames come at irregular intervals (every third frame is held longer)."""
    ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=s={size[0]}x{size[1]}:r=30:d={seconds}",
        "-vf", "setpts=PTS+floor(N/3)*0.05/TB", "-fps_mode", "vfr", *X264, str(path),
    )
    return path


def music_file(path: Path, seconds: float, frequency: int = 440) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration={seconds}", str(path))
    return path


def frame_at(path: Path, seconds: float, fps: float = 30) -> Image.Image:
    """The frame shown at `seconds`: the one with the largest timestamp not after it."""
    index = int(seconds * fps + 1e-6)
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", f"select=eq(n\\,{index})", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        check=True, stdin=subprocess.DEVNULL, capture_output=True,
    )
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def color_at(path: Path, seconds: float, point: tuple[float, float] = (0.5, 0.5)) -> tuple[int, int, int]:
    image = frame_at(path, seconds)
    return image.getpixel((int(image.width * point[0]), int(image.height * point[1])))


def close(color: tuple[int, int, int], expected: tuple[int, int, int], tolerance: int = 30) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(color, expected))


def probe_file(path: Path):
    return probe(path, RUNNER)


def video_element(source: str, start: float, end: float, *, dissolve: float = 0.0, **extra) -> PlanElement:
    transition = TransitionRequest(DISSOLVE, dissolve) if dissolve else TransitionRequest()
    return PlanElement(source=source, kind=VIDEO, duration=end - start, start=start, end=end, transition_in=transition, **extra)


def photo_element(source: str, duration: float, *, dissolve: float = 0.0, **extra) -> PlanElement:
    transition = TransitionRequest(DISSOLVE, dissolve) if dissolve else TransitionRequest()
    return PlanElement(source=source, kind=PHOTO, duration=duration, transition_in=transition, **extra)


def make_plan(elements: list[PlanElement], width: int = 320, height: int = 180, fps: float = 30.0, **extra) -> EditPlan:
    return EditPlan(output=OutputSpec(width, height, fps), elements=elements, **extra)


def fps_of(plan: EditPlan) -> Fraction:
    return plan.output.frame_rate


def halves_clip(path: Path, left: str, right: str, seconds: float, size=(320, 180), fps: int = 30, *, rotation: int = 0) -> Path:
    """Left half one color, right half another: shows how a picture was oriented and placed."""
    half = size[0] // 2
    plain = path if not rotation else path.with_name(f".{path.stem}.plain{path.suffix}")
    ffmpeg(
        "-f", "lavfi", "-i", f"color=c={left}:s={half}x{size[1]}:r={fps}:d={seconds}",
        "-f", "lavfi", "-i", f"color=c={right}:s={half}x{size[1]}:r={fps}:d={seconds}",
        "-filter_complex", f"[0:v][1:v]hstack,{BT709_FILTER}[v]", "-map", "[v]", *X264, str(plain),
    )
    if rotation:  # the tag is written in a second pass (FFmpeg 5.1 ignores it while encoding)
        ffmpeg("-i", str(plain), "-c", "copy", "-metadata:s:v:0", f"rotate={rotation}", str(path))
        plain.unlink()
    return path


def halves_photo(path: Path, left, right, size=(320, 240), *, exif_orientation: int | None = None,
                 border: tuple | None = None) -> Path:
    image = Image.new("RGB", size, left)
    image.paste(Image.new("RGB", (size[0] // 2, size[1]), right), (size[0] // 2, 0))
    if border:  # a sharp frame around the picture: a blurred copy of it never has pure border colors
        ImageDraw.Draw(image).rectangle((0, 0, size[0] - 1, size[1] - 1), outline=border, width=8)
    exif = Image.Exif()
    if exif_orientation:
        exif[0x0112] = exif_orientation
    if path.suffix.lower() in (".heic", ".heif"):
        import pillow_heif

        pillow_heif.register_heif_opener()
        image.save(path, format="HEIF", exif=exif.tobytes())
    else:
        image.save(path, quality=95, exif=exif)
    return path


def mean_luma(image: Image.Image, box: tuple[float, float, float, float]) -> float:
    """Mean gray level of a region given in fractions of the image (left, top, right, bottom)."""
    left, top, right, bottom = (int(v * s) for v, s in zip(box, (image.width, image.height) * 2))
    region = image.convert("L").crop((left, top, right, bottom))
    return sum(region.getdata()) / (region.width * region.height)


def volume(path: Path, start: float = 0.0, length: float | None = None) -> tuple[float, float]:
    """(mean, max) volume in dB of the audio, from FFmpeg's volumedetect."""
    window = ["-ss", str(start)] + (["-t", str(length)] if length else [])
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", *window, "-i", str(path), "-vn",
         "-af", "volumedetect", "-f", "null", "-"],
        check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    mean = float(result.stderr.split("mean_volume:")[1].split("dB")[0])
    peak = float(result.stderr.split("max_volume:")[1].split("dB")[0])
    return mean, peak
