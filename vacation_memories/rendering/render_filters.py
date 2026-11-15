"""FFmpeg filter graphs of the renderer, built as text from numbers computed in Python.

Nothing here runs FFmpeg: the functions return strings, so the geometry (what is scaled, cropped and
placed where) is covered by plain unit tests, and the real filters are exercised by the end-to-end
tests.
"""

from dataclasses import dataclass
from fractions import Fraction

from vacation_memories.planning.edit_plan import DIP, FADE_IN, FADE_OUT
from vacation_memories.planning.video_format import even
from vacation_memories.rendering.render_config import RenderConfig

ASPECT_TOLERANCE = 0.01  # a source this close to the output proportions is scaled, not put on a background
HDR_TO_SDR = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv"
)
OUTPUT_COLOR_ARGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


def even_down(value: float) -> int:
    return max(2, int(value // 2) * 2)


def fps_text(fps: Fraction) -> str:
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


def seconds_text(seconds: float) -> str:
    return f"{seconds:.6f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class Geometry:
    """How a source of `source` pixels (square, upright) is placed in a `target` frame."""

    source: tuple[int, int]
    target: tuple[int, int]

    @property
    def plain_scale(self) -> bool:
        src_w, src_h = self.source
        out_w, out_h = self.target
        return abs((src_w / src_h) / (out_w / out_h) - 1) <= ASPECT_TOLERANCE

    @property
    def foreground(self) -> tuple[int, int]:
        """The whole source, scaled to fit inside the target without distortion."""
        src_w, src_h = self.source
        out_w, out_h = self.target
        scale = min(out_w / src_w, out_h / src_h)
        return min(out_w, even(src_w * scale)), min(out_h, even(src_h * scale))

    @property
    def crop(self) -> tuple[int, int]:
        """Region of the source with the target proportions, centered, used for the background."""
        src_w, src_h = self.source
        out_w, out_h = self.target
        if src_w / src_h > out_w / out_h:
            return min(src_w, even_down(src_h * out_w / out_h)), src_h
        return src_w, min(src_h, even_down(src_w * out_h / out_w))


def compose(src_label: str, out_label: str, geometry: Geometry, config: RenderConfig, tag: str) -> list[str]:
    """Filter statements that fit the source in the target frame: scaled, or over a blurred copy.

    `tag` makes the intermediate labels unique inside a larger graph.
    """
    out_w, out_h = geometry.target
    if geometry.plain_scale:
        return [f"[{src_label}]scale={out_w}:{out_h}:flags=lanczos[{out_label}]"]

    fg_w, fg_h = geometry.foreground
    crop_w, crop_h = geometry.crop
    small_w, small_h = even(out_w / config.blur_divisor), even(out_h / config.blur_divisor)
    radius = max(1, min(4, min(small_w, small_h) // 4 - 1))
    x, y = (out_w - fg_w) // 2 // 2 * 2, (out_h - fg_h) // 2 // 2 * 2
    return [
        f"[{src_label}]split=2[{tag}src_bg][{tag}src_fg]",
        f"[{tag}src_bg]crop={crop_w}:{crop_h},scale={small_w}:{small_h}:flags=bilinear,"
        f"boxblur=luma_radius={radius}:luma_power=2,scale={out_w}:{out_h}:flags=bilinear[{tag}bg]",
        f"[{tag}src_fg]scale={fg_w}:{fg_h}:flags=lanczos[{tag}fg]",
        f"[{tag}bg][{tag}fg]overlay=x={x}:y={y}:format=yuv420[{out_label}]",
    ]


def video_segment_graph(
    *,
    stream_index: int = 0,
    stored_size: tuple[int, int],
    rotation: int,
    sample_aspect_ratio: Fraction,
    target: tuple[int, int],
    fps: Fraction,
    hdr_filter: str | None,
    config: RenderConfig,
) -> str:
    """Graph of a clip segment: timestamps from zero, constant frame rate, SDR, fitted in the frame.

    FFmpeg applies the rotation of the metadata itself before the graph (autorotate), so the graph
    sees an upright picture; `stored_size` and `rotation` only tell the geometry what that is.
    """
    width, height = (stored_size[1], stored_size[0]) if rotation % 180 else stored_size
    head = f"[0:{stream_index}]setpts=PTS-STARTPTS,fps={fps_text(fps)}"
    if sample_aspect_ratio != 1:
        width = even(width * float(sample_aspect_ratio))
        head += f",scale={width}:{height}:flags=bicubic,setsar=1"
    head += f",{hdr_filter}" if hdr_filter else ""
    head += ",format=yuv420p"
    geometry = Geometry((width, height), target)
    statements = [head + "[cfr]", *compose("cfr", "fitted", geometry, config, "v")]
    statements.append("[fitted]format=yuv420p,setsar=1[v]")
    return ";".join(statements)


def photo_segment_graph(
    *,
    photo_size: tuple[int, int],
    target: tuple[int, int],
    fps: Fraction,
    frames: int,
    zoom_in: bool,
    config: RenderConfig,
) -> str:
    """Graph of a photo segment: the photo fitted in an oversized canvas, then a slow zoom (zoompan)."""
    out_w, out_h = target
    factor = min(config.photo_overscale, config.photo_max_canvas_long_side / max(out_w, out_h))
    factor = max(1.0, factor)
    canvas = (even(out_w * factor), even(out_h * factor))

    steps = max(frames - 1, 1)
    progress = f"on/{steps}"
    zoom = f"1+{config.photo_zoom:g}*{progress}" if zoom_in else f"1+{config.photo_zoom:g}*(1-{progress})"
    statements = [
        # RGB to limited-range BT.709 here, so the colors match the tags written on the output
        "[0:v:0]scale=iw:ih:out_color_matrix=bt709:out_range=tv,format=yuv420p,setsar=1[yuv]",
        *compose("yuv", "canvas", Geometry(photo_size, canvas), config, "p"),
        f"[canvas]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={out_w}x{out_h}:"
        f"fps={fps_text(fps)},format=yuv420p,setsar=1,setpts=PTS-STARTPTS[v]",
    ]
    return ";".join(statements)


@dataclass(frozen=True)
class ClipFade:
    """A fade to or from black inside one clip: `start` and `frames` count frames from the clip's first one."""

    kind: str  # FADE_IN, FADE_OUT or DIP
    start: int
    frames: int


@dataclass(frozen=True)
class BlockItem:
    """One segment inside a block, the effective dissolve into it (0 = hard cut) and the fades inside it."""

    frames: int
    dissolve_frames: int
    fades: tuple[ClipFade, ...] = ()


def fade_statements(index: int, fades: tuple[ClipFade, ...]) -> tuple[list[str], str]:
    """The filters that apply the fades of clip `index` to its input, and the label of the result.

    Fades are counted in frames, so they do not depend on timestamps. A `dip` is a fade out of one copy
    of the clip and a fade in of another, shown one after the other (`overlay` with an `enable`
    window, so no `concat` changes the time base that `xfade` needs).
    """
    statements: list[str] = []
    current = f"{index}:v"
    for number, fade in enumerate(sorted(fades, key=lambda item: item.start)):
        tag = f"e{index}_{number}"
        label = f"e{index}" if number == len(fades) - 1 else tag
        if fade.kind == FADE_IN:
            statements.append(f"[{current}]fade=t=in:s={fade.start}:n={fade.frames}[{label}]")
        elif fade.kind == FADE_OUT:
            statements.append(f"[{current}]fade=t=out:s={fade.start}:n={fade.frames}[{label}]")
        elif fade.kind == DIP:
            down = fade.frames // 2
            bottom = fade.start + down
            statements += [
                f"[{current}]split=2[{tag}a][{tag}b]",
                f"[{tag}a]fade=t=out:s={fade.start}:n={down}[{tag}ao]",
                f"[{tag}b]fade=t=in:s={bottom}:n={fade.frames - down}[{tag}bo]",
                f"[{tag}ao][{tag}bo]overlay=enable='gte(n,{bottom})':format=yuv420[{label}]",
            ]
        else:
            raise ValueError(f"unknown fade {fade.kind!r}")
        current = label
    return statements, current


def block_graph(items: list[BlockItem], fps: Fraction, out_label: str = "vout", output_filters: str = "") -> str:
    """Join segments: dissolves with `xfade`, hard cuts with `concat`; the fades of a clip come first.

    A run of segments joined by dissolves becomes one `xfade` chain; the runs are then joined by a
    single `concat`, so `xfade` never receives the output of `concat` (their time bases differ).
    Offsets are placed a quarter of a frame early so that rounding cannot move the first blended frame.
    """
    statements: list[str] = []
    labels = []
    for index, item in enumerate(items):
        faded, label = fade_statements(index, item.fades)
        statements += faded
        labels.append(label)

    runs: list[list[int]] = []
    for index, item in enumerate(items):
        if index and item.dissolve_frames:
            runs[-1].append(index)
        else:
            runs.append([index])

    run_labels = []
    for run_number, run in enumerate(runs):
        current = labels[run[0]]
        length = items[run[0]].frames
        for position, index in enumerate(run[1:], start=1):
            dissolve = items[index].dissolve_frames
            offset = (length - dissolve - 0.25) / fps
            label = f"r{run_number}x{position}"
            statements.append(
                f"[{current}][{labels[index]}]xfade=transition=fade:duration={seconds_text(float(dissolve / fps))}:"
                f"offset={seconds_text(float(offset))}[{label}]"
            )
            current = label
            length += items[index].frames - dissolve
        run_labels.append(current)

    if len(run_labels) > 1:
        joined = "".join(f"[{label}]" for label in run_labels)
        statements.append(f"{joined}concat=n={len(run_labels)}:v=1:a=0[joined]")
        last = "joined"
    else:
        last = run_labels[0]

    # No `fps` filter here: after `xfade` it drops the last frame, and the inputs are already
    # on the frame grid (the output encoder still enforces a constant rate).
    tail = "format=yuv420p,setsar=1"
    if output_filters:  # e.g. the upload to the GPU of a hardware encoder
        tail += f",{output_filters}"
    statements.append(f"[{last}]{tail}[{out_label}]")
    return ";".join(statements)


def audio_graph(
    *,
    duration: float,
    sample_rate: int,
    fade_in: float,
    fade_out: float,
    music_inputs: list[int],
    crossfade: float,
    out_label: str = "aout",
) -> str:
    """Audio of the video: silence, or the music trimmed (or looped with crossfades) to `duration`.

    `music_inputs` are the FFmpeg input numbers of the same track, repeated when it is shorter than
    the video and has to loop.
    """
    layout = "stereo"
    if not music_inputs:
        source = f"anullsrc=channel_layout={layout}:sample_rate={sample_rate}"
        return f"{source},atrim=duration={seconds_text(duration)},asetpts=PTS-STARTPTS[{out_label}]"

    statements = []
    labels = []
    for index in music_inputs:
        label = f"m{index}"
        statements.append(
            f"[{index}:a]aresample={sample_rate},aformat=sample_fmts=fltp:channel_layouts={layout}[{label}]"
        )
        labels.append(label)
    current = labels[0]
    for number, label in enumerate(labels[1:], start=1):
        joined = f"loop{number}"
        statements.append(f"[{current}][{label}]acrossfade=d={seconds_text(crossfade)}:c1=tri:c2=tri[{joined}]")
        current = joined
    chain = f"atrim=duration={seconds_text(duration)},asetpts=PTS-STARTPTS"
    fade_in = min(fade_in, duration / 2)
    fade_out = min(fade_out, duration / 2)
    if fade_in > 0:
        chain += f",afade=t=in:st=0:d={seconds_text(fade_in)}"
    if fade_out > 0:
        chain += f",afade=t=out:st={seconds_text(duration - fade_out)}:d={seconds_text(fade_out)}"
    statements.append(f"[{current}]{chain}[{out_label}]")
    return ";".join(statements)


@dataclass(frozen=True)
class MixPart:
    """A part of a track of the plan, read from FFmpeg input number `input`: seconds of the track, fades of the part."""

    input: int
    start: float
    end: float
    muted: bool
    fade_in: float
    fade_out: float


def parts_audio_graph(
    parts: list[MixPart],
    *,
    offset: float,
    duration: float,
    sample_rate: int,
    fade_out: float,
    out_label: str = "aout",
) -> str:
    """Audio of the video from the plan's music: the parts one after the other, shifted, fitted to `duration`.

    A positive `offset` puts silence in front, a negative one cuts that much from the start of the
    music. What is left over at the end is cut off, and a music that is too short is padded with
    silence; the music ends with a fade out of `fade_out` seconds, the video's last one.
    """
    layout = "stereo"
    statements, labels = [], []
    for number, part in enumerate(parts):
        label = f"p{number}"
        length = part.end - part.start
        chain = (
            f"[{part.input}:a]aresample={sample_rate},aformat=sample_fmts=fltp:channel_layouts={layout},"
            f"atrim=start={seconds_text(part.start)}:end={seconds_text(part.end)},asetpts=PTS-STARTPTS"
        )
        if part.muted:
            chain += ",volume=0"
        if part.fade_in > 0:
            chain += f",afade=t=in:st=0:d={seconds_text(part.fade_in)}"
        if part.fade_out > 0:
            chain += f",afade=t=out:st={seconds_text(length - part.fade_out)}:d={seconds_text(part.fade_out)}"
        statements.append(f"{chain}[{label}]")
        labels.append(label)

    if offset > 0:
        statements.append(
            f"anullsrc=channel_layout={layout}:sample_rate={sample_rate},atrim=duration={seconds_text(offset)},"
            "asetpts=PTS-STARTPTS[lead]"
        )
        labels.insert(0, "lead")
    joined = "".join(f"[{label}]" for label in labels)
    if offset < 0:
        statements.append(f"{joined}concat=n={len(labels)}:v=0:a=1[joined]")
        statements.append(f"[joined]atrim=start={seconds_text(-offset)},asetpts=PTS-STARTPTS[mix]")
    else:
        statements.append(f"{joined}concat=n={len(labels)}:v=0:a=1[mix]")

    tail = f"apad=whole_dur={seconds_text(duration)},atrim=duration={seconds_text(duration)},asetpts=PTS-STARTPTS"
    fade_out = min(fade_out, duration / 2)
    if fade_out > 0:
        tail += f",afade=t=out:st={seconds_text(duration - fade_out)}:d={seconds_text(fade_out)}"
    statements.append(f"[mix]{tail}[{out_label}]")
    return ";".join(statements)
