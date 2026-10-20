"""H.264 encoder settings and the choice between software and hardware encoding.

Segments are always encoded in software (`libx264`): they are cut and filtered on the CPU anyway
and must be predictable. Only the assembly (blocks and final file) may use a hardware encoder, and
only when it is asked for with `--hwaccel` and a short test encode proves that it works here. When
anything is missing the renderer falls back to `libx264` and says why.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from vacation_memories.process import Interrupted, ToolError, ToolRunner
from vacation_memories.render_config import RenderConfig
from vacation_memories.render_filters import OUTPUT_COLOR_ARGS

SOFTWARE_ENCODER = "libx264"
FPS_MODE_FIRST_VERSION = (5, 1)  # `-fps_mode` replaces the deprecated `-vsync` from FFmpeg 5.1
TEST_ENCODE_TIMEOUT_SECONDS = 30
VAAPI_DEVICE_GLOB = "renderD*"


@dataclass(frozen=True)
class Encoder:
    """What to add to an FFmpeg command to encode H.264 video."""

    name: str
    codec_args: list[str]
    input_args: list[str] = field(default_factory=list)  # before the inputs (e.g. the VAAPI device)
    filter_suffix: str = ""  # appended to the video chain (e.g. the upload to the GPU)

    @property
    def is_software(self) -> bool:
        return self.name == SOFTWARE_ENCODER


def thread_args(threads: int | None) -> tuple[list[str], list[str], list[str]]:
    """Thread options for decoders (before each input), filters (global) and the encoder (before the output)."""
    if not threads:
        return [], [], []
    count = str(threads)
    return ["-threads", count], ["-filter_complex_threads", count], ["-threads", count]


def fps_mode_args(ffmpeg_version: str | None) -> list[str]:
    """`-fps_mode cfr`, or `-vsync cfr` on FFmpeg older than 5.1 (builds of unknown version count as new)."""
    match = re.match(r"n?(\d+)\.(\d+)", ffmpeg_version or "")
    if match and (int(match.group(1)), int(match.group(2))) < FPS_MODE_FIRST_VERSION:
        return ["-vsync", "cfr"]
    return ["-fps_mode", "cfr"]


def software_encoder(preset: str, crf: int, *, conservative: bool = False, gop: int | None = None) -> Encoder:
    args = ["-c:v", SOFTWARE_ENCODER, "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", *OUTPUT_COLOR_ARGS]
    if conservative:
        args += ["-bf", "0"]
    if gop:
        args += ["-g", str(gop)]
    return Encoder(SOFTWARE_ENCODER, args)


def hardware_candidates(requested: str, available: tuple[str, ...]) -> list[str]:
    """Hardware H.264 encoders to try for `--hwaccel` (`auto` or an accelerator name)."""
    suffixes = ("vaapi", "qsv", "nvenc") if requested == "auto" else (requested,)
    return [name for suffix in suffixes for name in available if name == f"h264_{suffix}"]


def _hardware_encoder(name: str, config: RenderConfig) -> Encoder | None:
    if name == "h264_vaapi":
        devices = sorted(Path("/dev/dri").glob(VAAPI_DEVICE_GLOB)) if Path("/dev/dri").is_dir() else []
        if not devices:
            return None
        return Encoder(
            name,
            ["-c:v", name, "-qp", str(config.final_crf + 4), *OUTPUT_COLOR_ARGS],
            input_args=["-vaapi_device", str(devices[0])],
            filter_suffix="format=nv12,hwupload",
        )
    if name == "h264_qsv":
        return Encoder(name, ["-c:v", name, "-global_quality", str(config.final_crf + 4), "-preset", "fast", *OUTPUT_COLOR_ARGS])
    if name == "h264_nvenc":
        return Encoder(name, ["-c:v", name, "-preset", "p4", "-cq", str(config.final_crf + 4), *OUTPUT_COLOR_ARGS])
    return None


def choose_assembly_encoder(
    hwaccel: str, hw_encoders: tuple[str, ...], runner: ToolRunner, config: RenderConfig, *, preview: bool
) -> tuple[Encoder, str | None]:
    """The encoder for the blocks and the final file, and why it is not the hardware one if requested."""
    preset, crf = (config.preview_preset, config.preview_crf) if preview else (config.final_preset, config.final_crf)
    software = software_encoder(preset, crf)
    if hwaccel == "none":
        return software, None
    candidates = hardware_candidates(hwaccel, hw_encoders)
    if not candidates:
        return software, f"no hardware H.264 encoder matches --hwaccel {hwaccel}"
    reasons = []
    for name in candidates:
        encoder = _hardware_encoder(name, config)
        if encoder is None:
            reasons.append(f"{name}: no device available")
            continue
        problem = _test_encode(encoder, runner)
        if problem is None:
            return encoder, None
        reasons.append(f"{name}: {problem}")
    return software, "hardware encoding does not work here (" + "; ".join(reasons) + ")"


def _test_encode(encoder: Encoder, runner: ToolRunner) -> str | None:
    """Encode half a second of a generated picture; None if it works, else a short reason."""
    chain = "format=yuv420p" + (f",{encoder.filter_suffix}" if encoder.filter_suffix else "")
    args = [
        *encoder.input_args,
        "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=10:d=0.5",
        "-vf", chain, *encoder.codec_args, "-frames:v", "5", "-f", "null", "-",
    ]
    try:
        runner.ffmpeg(args, timeout=TEST_ENCODE_TIMEOUT_SECONDS)
    except Interrupted:
        raise
    except ToolError as error:
        lines = str(error).strip().splitlines()
        return lines[-1] if lines else "test encode failed"
    return None
