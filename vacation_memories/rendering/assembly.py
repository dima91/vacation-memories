"""Phase B of rendering: segments joined with dissolves and cuts, with the clips' fades, then the music.

Long timelines are assembled in blocks of limited size (one filter graph with hundreds of inputs
would exhaust memory): every block is encoded on its own, then the blocks are joined and the audio
is added in one last pass. A timeline that fits in one block is assembled and finished in a
single pass. A dissolve across a block boundary is made in the last pass, which joins the blocks the
way a block joins its segments (see `transitions.split_blocks`). The fades of a clip are made where
its segment is joined, so they are in the block files and the last pass only joins them.
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vacation_memories.core.process import ToolRunner
from vacation_memories.rendering.encoders import Encoder, fps_mode_args, software_encoder, thread_args
from vacation_memories.rendering.music import LoopedTrack, PartsMusic
from vacation_memories.rendering.render_config import RenderConfig
from vacation_memories.rendering.render_filters import (
    BlockItem,
    MixPart,
    audio_graph,
    block_graph,
    fps_text,
    parts_audio_graph,
)


@dataclass(frozen=True)
class MusicFades:
    """Seconds of the fade in and fade out of the music of a `--music` track, and of the plan's final fade out."""

    fade_in: float
    fade_out: float


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
        self._run(
            inputs, items, target, software_encoder(preset, crf), total, music=None, fades=None, audio=False, final=False
        )

    def render_final(
        self,
        inputs: list[Path],
        items: list[BlockItem],
        target: Path,
        encoder: Encoder,
        music: LoopedTrack | PartsMusic | None,
        fades: MusicFades,
    ) -> None:
        """Join the inputs, add the music (or silence) and write the file."""
        total = sum(item.frames - item.dissolve_frames for item in items)
        self._run(inputs, items, target, encoder, total, music=music, fades=fades, audio=True, final=True)

    def _run(
        self, inputs, items, target: Path, encoder: Encoder, total: int, *, music, fades: MusicFades | None,
        audio: bool, final: bool,
    ) -> None:
        duration = float(total / self.fps)
        graph = block_graph(items, self.fps, output_filters=encoder.filter_suffix)

        decode, filtering, encode = thread_args(self.threads)
        arguments = list(encoder.input_args)
        for path in inputs:
            arguments += [*decode, "-i", str(path)]
        maps = ["-map", "[vout]"]
        if audio:
            graph += ";" + self._audio(arguments, len(inputs), music, fades, duration)
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

    def _audio(self, arguments: list[str], first: int, music, fades: MusicFades, duration: float) -> str:
        """The audio graph, and the music files as FFmpeg inputs (appended to `arguments`, from input `first`)."""
        rate = self.config.audio_sample_rate
        if isinstance(music, PartsMusic):
            parts = []
            for number, part in enumerate(music.parts):
                arguments += ["-i", str(part.path)]
                parts.append(MixPart(first + number, part.start, part.end, part.muted, part.fade_in, part.fade_out))
            return parts_audio_graph(parts, offset=music.offset, duration=duration, sample_rate=rate,
                                     fade_out=fades.fade_out)
        music_inputs: list[int] = []
        if music is not None:
            music_inputs = list(range(first, first + music.repeats))
            for _ in music_inputs:
                arguments += ["-i", str(music.path)]
        return audio_graph(
            duration=duration,
            sample_rate=rate,
            fade_in=fades.fade_in,
            fade_out=fades.fade_out,
            music_inputs=music_inputs,
            crossfade=music.crossfade if music else 0.0,
        )
