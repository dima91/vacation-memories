"""Phase B of rendering: segments joined with dissolves and cuts, then the music.

Long timelines are assembled in blocks of limited size (one filter graph with hundreds of inputs
would exhaust memory): every block is encoded on its own, then the blocks are concatenated and the
audio is added in one last pass. A timeline that fits in one block is assembled and finished in a
single pass. Block boundaries fall on hard cuts (see `transitions.split_blocks`).
"""

import logging
import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vacation_memories.encoders import Encoder, fps_mode_args, software_encoder, thread_args
from vacation_memories.media_probe import ProbeError, probe
from vacation_memories.process import ToolRunner
from vacation_memories.render_config import RenderConfig
from vacation_memories.render_filters import BlockItem, audio_graph, block_graph, fps_text

logger = logging.getLogger(__name__)


class MusicError(Exception):
    """The music file cannot be used."""


@dataclass(frozen=True)
class MusicPlan:
    path: Path
    track_duration: float
    mode: str  # "trimmed" (longer track, or equal), "looped" (shorter track)
    repeats: int
    crossfade: float


def music_track_duration(path: Path, runner: ToolRunner) -> float:
    """Length of the music file in seconds; checked before any rendering starts."""
    try:
        info = probe(path, runner)
    except ProbeError as error:
        raise MusicError(f"cannot read the music file {path}: {error}") from error
    if info.audio is None:
        raise MusicError(f"the music file {path} has no audio stream")
    track = info.audio.duration or info.format_duration
    if not track or track <= 0:
        raise MusicError(f"cannot tell how long the music file {path} is")
    return track


def plan_music(path: Path, track: float, video_duration: float, config: RenderConfig) -> MusicPlan:
    """Decide how the track fits the video: cut with a fade-out, or looped with crossfades."""
    if track >= video_duration:
        return MusicPlan(path, track, "trimmed", 1, 0.0)

    crossfade = min(config.music_loop_crossfade_seconds, track / 4)
    repeats = math.ceil((video_duration - crossfade) / (track - crossfade))
    logger.warning(
        "the music (%.1f s) is shorter than the video (%.1f s): it loops %d times with %.1f s crossfades",
        track, video_duration, repeats, crossfade,
    )
    return MusicPlan(path, track, "looped", repeats, crossfade)


class Assembler:
    def __init__(
        self,
        *,
        runner: ToolRunner,
        config: RenderConfig,
        fps: Fraction,
        ffmpeg_version: str | None,
        threads: int | None,
    ):
        self.runner = runner
        self.config = config
        self.fps = fps
        self.ffmpeg_version = ffmpeg_version
        self.threads = threads

    def render_block(self, inputs: list[Path], items: list[BlockItem], target: Path, preview: bool) -> None:
        """Join some segments into an intermediate file, encoded like the segments."""
        preset, crf = (
            (self.config.preview_preset, self.config.preview_crf)
            if preview
            else (self.config.segment_preset, self.config.segment_crf)
        )
        total = sum(item.frames - item.dissolve_frames for item in items)
        self._run(inputs, items, target, software_encoder(preset, crf), total, music=None, audio=False, fades=False, final=False)

    def render_final(
        self,
        inputs: list[Path],
        items: list[BlockItem],
        target: Path,
        encoder: Encoder,
        music: MusicPlan | None,
    ) -> None:
        """Join the inputs, apply the video fades, add the music (or silence) and write the file."""
        total = sum(item.frames - item.dissolve_frames for item in items)
        self._run(inputs, items, target, encoder, total, music=music, audio=True, fades=True, final=True)

    def _run(
        self, inputs, items, target: Path, encoder: Encoder, total: int, *, music, audio: bool, fades: bool, final: bool
    ) -> None:
        duration = float(total / self.fps)
        fade_in = min(self.config.fade_in_seconds, duration / 2) if fades else 0.0
        fade_out = min(self.config.fade_out_seconds, duration / 2) if fades else 0.0
        graph = block_graph(items, self.fps, fade_in, fade_out, total, output_filters=encoder.filter_suffix)

        decode, filtering, encode = thread_args(self.threads)
        arguments = list(encoder.input_args)
        for path in inputs:
            arguments += [*decode, "-i", str(path)]
        maps = ["-map", "[vout]"]
        if audio:
            music_inputs: list[int] = []
            if music is not None:
                first = len(inputs)
                music_inputs = list(range(first, first + music.repeats))
                for _ in music_inputs:
                    arguments += ["-i", str(music.path)]
            graph += ";" + audio_graph(
                duration=duration,
                sample_rate=self.config.audio_sample_rate,
                fade_in=fade_in,
                fade_out=fade_out,
                music_inputs=music_inputs,
                crossfade=music.crossfade if music else 0.0,
            )
            maps += ["-map", "[aout]"]

        arguments += [*filtering, "-filter_complex", graph, *maps, "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn"]
        arguments += ["-frames:v", str(total), *encoder.codec_args, *fps_mode_args(self.ffmpeg_version)]
        arguments += ["-r", fps_text(self.fps), "-video_track_timescale", str(self.config.video_timescale)]
        if audio:
            arguments += [
                "-c:a", "aac", "-b:a", self.config.audio_bitrate,
                "-ar", str(self.config.audio_sample_rate), "-ac", "2",
            ]
        if final:
            arguments += ["-movflags", "+faststart"]
        arguments += [*encode, str(target)]
        self.runner.ffmpeg(arguments)
