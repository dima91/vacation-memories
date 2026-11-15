"""The music of a render: which files play, read and checked before any work is done.

Two sources (section 9 of `SPEC.md`, decision D6 of `UI-SPEC.md`):

- the `music` block of the plan: tracks in order, each cut into parts (`start`, `end` in seconds of the
  track, `muted`, `fade_in`, `fade_out`), and a global `offset`. The files are relative to the music
  folder. This is what the editor writes;
- the `--music` file, for a plan without a `music` block: one track, cut with a fade out when it is
  longer than the video and looped with crossfades when it is shorter (Milestone 1).

A plan with a `music` block ignores `--music` (with a warning); a block without tracks is a video
without music. What a hand-written plan gets wrong (a part past the end of its track, fades longer than
the part) is reduced with a warning; a file that cannot be read stops the render (exit code 2).
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vacation_memories.analysis.media_probe import ProbeError, probe
from vacation_memories.core.process import ToolRunner
from vacation_memories.planning.edit_plan import MusicPlan as PlanMusic
from vacation_memories.rendering.render_config import RenderConfig

logger = logging.getLogger(__name__)

MIN_PART_SECONDS = 0.05  # a part shorter than this (after the cut to its track) is left out


class MusicError(Exception):
    """The music file cannot be used."""


@dataclass(frozen=True)
class TrackSource:
    """The `--music` file and how long it is."""

    path: Path
    duration: float


@dataclass(frozen=True)
class LoopedTrack:
    """The `--music` file fitted to the video: cut with a fade out ("trimmed"), or looped with crossfades."""

    path: Path
    track_duration: float
    mode: str  # "trimmed" (longer track, or equal), "looped" (shorter track)
    repeats: int
    crossfade: float


@dataclass(frozen=True)
class PartSource:
    """A part of a track of the plan, on disk."""

    path: Path
    start: float
    end: float
    muted: bool
    fade_in: float
    fade_out: float


@dataclass(frozen=True)
class PartsMusic:
    """The music block of the plan: its parts in playing order, the shift, and the files for the report."""

    parts: tuple[PartSource, ...]
    offset: float
    files: tuple[str, ...]  # the tracks, relative to the music folder


def music_track_duration(path: Path, runner: ToolRunner, name: str | None = None) -> float:
    """Length of a music file in seconds; checked before any rendering starts. `name` is how errors call it."""
    name = name or str(path)
    try:
        info = probe(path, runner)
    except ProbeError as error:
        raise MusicError(f"cannot read the music file {name}: {error}") from error
    if info.audio is None:
        raise MusicError(f"the music file {name} has no audio stream")
    track = info.audio.duration or info.format_duration
    if not track or track <= 0:
        raise MusicError(f"cannot tell how long the music file {name} is")
    return track


def load_music(
    music: PlanMusic | None,
    track: Path | None,
    music_dir: Path | None,
    runner: ToolRunner,
    warn: Callable[[str], None],
) -> TrackSource | PartsMusic | None:
    """The music this render plays: the plan's block, else the `--music` file, else none."""
    if music is None:
        if track is None:
            return None
        return TrackSource(track, music_track_duration(track, runner))
    if track is not None:
        warn("--music is ignored: the plan has its own music")
    if not music.tracks:
        return None
    if music_dir is None:
        raise MusicError("the plan has music, but no music folder is set (music_dir in the configuration)")

    parts: list[PartSource] = []
    durations: dict[str, float] = {}
    for number, entry in enumerate(music.tracks):
        path = music_dir / entry.file
        if entry.file not in durations:
            durations[entry.file] = music_track_duration(path, runner, entry.file)
        known = durations[entry.file]
        for position, part in enumerate(entry.parts):
            where = f"music.tracks[{number}].parts[{position}] ({entry.file})"
            fitted = _fit_part(part, path, known, where, warn)
            if fitted is not None:
                parts.append(fitted)
    return PartsMusic(tuple(parts), music.offset, tuple(entry.file for entry in music.tracks))


def _fit_part(part, path: Path, track_duration: float, where: str, warn: Callable[[str], None]) -> PartSource | None:
    end = min(part.end, track_duration)
    if end - part.start < MIN_PART_SECONDS:
        warn(f"{where}: starts at {part.start:g} s but the track lasts {track_duration:.1f} s: part left out")
        return None
    if end < part.end:
        warn(f"{where}: the part ends after the track ({part.end:g} s of {track_duration:.1f} s): cut at its end")
    fade_in, fade_out = part.fade_in, part.fade_out
    length = end - part.start
    if fade_in + fade_out > length:
        scale = length / (fade_in + fade_out)
        warn(f"{where}: the fades are longer than the part: reduced to fit")
        fade_in, fade_out = fade_in * scale, fade_out * scale
    return PartSource(path, part.start, end, part.muted, fade_in, fade_out)


def plan_track(source: TrackSource, video_duration: float, config: RenderConfig) -> LoopedTrack:
    """Decide how the `--music` file fits the video: cut with a fade-out, or looped with crossfades."""
    if source.duration >= video_duration:
        return LoopedTrack(source.path, source.duration, "trimmed", 1, 0.0)

    crossfade = min(config.music_loop_crossfade_seconds, source.duration / 4)
    repeats = math.ceil((video_duration - crossfade) / (source.duration - crossfade))
    logger.warning(
        "the music (%.1f s) is shorter than the video (%.1f s): it loops %d times with %.1f s crossfades",
        source.duration, video_duration, repeats, crossfade,
    )
    return LoopedTrack(source.path, source.duration, "looped", repeats, crossfade)
