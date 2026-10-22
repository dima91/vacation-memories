"""`bench vision`: how long the vision model takes per frame on this machine.

By default it uses a few synthetic images; `--images` takes the photos of a folder instead (in the
compose file, a folder under `/media`). It never uses the analysis cache, so every run measures the
model, and it only reads the folder it is given. The first frame includes loading the model, so it is
reported apart from the others.
"""

import logging
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from vacation_memories import exit_codes
from vacation_memories.config import ConfigError, Settings
from vacation_memories.frame_sampler import SamplingError, photo_color_frame
from vacation_memories.media_scanner import scan_media
from vacation_memories.ollama_client import OllamaClient
from vacation_memories.runtime_profile import (
    DetectionEnv,
    RuntimeProfile,
    loaded_model_placement,
    record_model_placement,
)
from vacation_memories.synthetic_media import render_scene_frame
from vacation_memories.vision_analyzer import VisionAnalyzer

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_COUNT = 5
SYNTHETIC_SIZE = (640, 480)


def run_bench_vision(
    settings: Settings,
    profile: RuntimeProfile,
    detection_env: DetectionEnv | None,
    *,
    images: Path | None,
    count: int,
    clock: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = print,
) -> int:
    if settings.no_ai:
        raise ConfigError("bench vision measures the AI: it cannot be used with --no-ai")
    if count < 1:
        raise ConfigError("--count must be at least 1")
    frames = _frame_source(images, count)

    ollama = profile.ollama
    if ollama is None or not ollama.reachable:
        print(f"error: Ollama is not reachable at {settings.ollama_url}", file=sys.stderr)
        return exit_codes.REQUIREMENTS
    if not ollama.model_present:
        print(f"error: the model {settings.model} is not installed in Ollama (see `ollama list`)", file=sys.stderr)
        return exit_codes.REQUIREMENTS

    client = OllamaClient(settings.ollama_url, settings.model, timeout=settings.ai_timeout)
    timings: list[float] = []
    placement = "unknown"
    out(f"Model {settings.model}, images resized to {settings.ai_resolution} px, {len(frames)} frame(s)")
    with VisionAnalyzer(
        client, model=settings.model, resolution=settings.ai_resolution, unload_model=settings.ai_unload_model
    ) as analyzer:
        for number, load in enumerate(frames, start=1):
            label = f"frame {number}/{len(frames)}"
            try:
                image = load()
            except SamplingError as error:
                out(f"{label}: unreadable ({error})")
                continue
            start = clock()
            result = analyzer.analyze_image(image)
            seconds = clock() - start
            if result is None:
                out(f"{label}: failed")
                continue
            timings.append(seconds)
            out(f"{label}: {seconds:.2f} s  {_describe(result)}")
            if len(timings) == 1:  # the model is loaded now
                placement = loaded_model_placement(detection_env or DetectionEnv(), settings.ollama_url, settings.model)
                record_model_placement(settings.workdir / "runtime_profile.json", placement)

    if not timings:
        print("error: no frame could be analyzed", file=sys.stderr)
        return exit_codes.ERROR
    for line in _summary(timings, placement):
        out(line)
    return exit_codes.OK


def _describe(result) -> str:
    fields = result.to_dict()
    text = " ".join(f"{name}={fields[name]}" for name in ("moment_type", "narrative_value", "category", "people"))
    return f'{text}  "{fields["description"] or ""}"'


def _summary(timings: list[float], placement: str) -> list[str]:
    lines = [f"first frame (includes loading the model): {timings[0]:.2f} s"]
    rest = timings[1:]
    if rest:
        lines.append(
            f"seconds per frame (without the first): median {statistics.median(rest):.2f} s, "
            f"mean {statistics.fmean(rest):.2f} s, min {min(rest):.2f} s, max {max(rest):.2f} s"
        )
    else:
        lines.append("seconds per frame: only one frame was measured, ask for more with --count")
    lines.append(f"model runs on: {placement}")
    return lines


def _frame_source(images: Path | None, count: int) -> list[Callable[[], Image.Image]]:
    """One loader per frame, so photos are read one at a time."""
    if images is None:
        return [lambda scene=scene: render_scene_frame(scene, *SYNTHETIC_SIZE) for scene in range(1, count + 1)]
    try:
        photos = [media for media in scan_media(images) if media.kind == "photo"]
    except (FileNotFoundError, NotADirectoryError) as error:
        raise ConfigError(str(error)) from None
    if not photos:
        raise ConfigError(f"no photos found in {images}")
    chosen = photos if len(photos) <= count else [photos[i * len(photos) // count] for i in range(count)]
    return [lambda path=media.path: photo_color_frame(path) for media in chosen]
