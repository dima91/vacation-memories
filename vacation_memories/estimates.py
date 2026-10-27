"""Rough time and space estimates of a render, from a short test encode on this machine.

A few seconds of a textured synthetic clip are encoded at the output size with the final encoder
settings; time and size per second of video are then scaled to the planned duration. The segments
are encoded once more before the assembly, so the estimate counts the work twice. It is an order
of magnitude, not a promise: real material, photos with the Ken Burns effect and the hardware
encoder change it.
"""

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from vacation_memories.edit_plan import OutputSpec
from vacation_memories.process import ToolError, ToolRunner
from vacation_memories.render_config import RenderConfig

TEST_SECONDS = 2.0
SEGMENT_SPACE_FACTOR = 2.0  # segments use a lower CRF than the final file


@dataclass(frozen=True)
class RenderEstimate:
    seconds: float
    final_bytes: int
    work_bytes: int  # segments and temporary blocks in the workdir

    def describe(self) -> str:
        return (f"rendering about {_duration(self.seconds)}, final file about {_size(self.final_bytes)}, "
                f"plus about {_size(self.work_bytes)} of segments in the workdir")


def estimate_render(
    spec: OutputSpec, planned_seconds: float, jobs: int, config: RenderConfig, runner: ToolRunner, *,
    preview: bool = False, tmp_dir: Path | None = None,
) -> RenderEstimate | None:
    """None if the test encode fails (the estimate is informative only)."""
    preset, crf = (config.preview_preset, config.preview_crf) if preview else (config.final_preset, config.final_crf)
    with tempfile.TemporaryDirectory(prefix="estimate-", dir=tmp_dir) as folder:
        target = Path(folder) / "test.mp4"
        source = f"testsrc2=s={spec.width}x{spec.height}:r={spec.fps:g}:d={TEST_SECONDS:g}"
        started = time.monotonic()
        try:
            runner.ffmpeg(["-f", "lavfi", "-i", source, "-vf", "noise=alls=10", "-c:v", "libx264",
                           "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-an", str(target)])
        except ToolError:
            return None
        elapsed = time.monotonic() - started
        size = target.stat().st_size
    per_second, bytes_per_second = elapsed / TEST_SECONDS, size / TEST_SECONDS
    seconds = planned_seconds * per_second * (1 + 1 / max(1, jobs))
    final = int(planned_seconds * bytes_per_second)
    return RenderEstimate(seconds, final, int(final * SEGMENT_SPACE_FACTOR))


def _duration(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.0f} min"


def _size(size: int) -> str:
    return f"{size / 1024**2:.0f} MB" if size < 1024**3 else f"{size / 1024**3:.1f} GB"
