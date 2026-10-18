"""Fake machine for the detection tests: /proc/meminfo, cgroup files, FFmpeg and Ollama answers."""

from pathlib import Path

from vacation_memories.runtime_profile import DetectionEnv

GIB = 1024**3
MODEL = "qwen3-vl:4b-instruct-q4_K_M"
MODEL_SIZE = 3_300_000_000

FULL_FILTERS = (
    "xfade zoompan fps scale boxblur overlay format setsar afade acrossfade anullsrc zscale tonemap".split()
)
BASE_ENCODERS = ("libx264", "aac")

_ENCODERS_TEMPLATE = """Encoders:
 V..... = Video
 A..... = Audio
 ------
{lines}
"""
_FILTERS_TEMPLATE = """Filters:
  T.. = Timeline support
  ..C = Command support
  A = Audio input/output
  V = Video input/output
 ------
{lines}
"""


def encoders_output(names) -> str:
    lines = "\n".join(f" {'A' if name == 'aac' else 'V'}....D {name:<20} Some encoder" for name in names)
    return _ENCODERS_TEMPLATE.format(lines=lines)


def filters_output(names) -> str:
    lines = "\n".join(f" T.. {name:<18} V->V       Some filter" for name in names)
    return _FILTERS_TEMPLATE.format(lines=lines)


def hwaccels_output(names) -> str:
    return "Hardware acceleration methods:\n" + "".join(f"{name}\n" for name in names)


def make_env(
    root: Path,
    *,
    cores: int | Exception = 8,
    mem_available_bytes: int | None = 16 * GIB,
    encoders=BASE_ENCODERS,
    filters=FULL_FILTERS,
    hwaccels=(),
    ffmpeg_version="5.1.6-0+deb12u1",
    ffprobe=True,
    ffmpeg=True,
    ollama: dict | None = None,
    disk_free: int | Exception = 500 * GIB,
) -> DetectionEnv:
    """Build a `DetectionEnv` backed by files under `root` and canned command/HTTP answers.

    `ollama` is `{"tags": [...], "ps": [...]}` as returned by `/api/tags` and `/api/ps`;
    None means the server is unreachable.
    """
    root.mkdir(parents=True, exist_ok=True)
    meminfo = root / "meminfo"
    if mem_available_bytes is not None:
        meminfo.write_text(
            f"MemTotal:       {64 * GIB // 1024} kB\nMemAvailable:   {mem_available_bytes // 1024} kB\n"
        )
    cgroup_root = root / "cgroup"
    cgroup_root.mkdir(exist_ok=True)

    commands: list[list[str]] = []
    http_calls: list[str] = []

    def cpu_affinity() -> int:
        if isinstance(cores, Exception):
            raise cores
        return cores

    def disk_usage(path: Path) -> int:
        if isinstance(disk_free, Exception):
            raise disk_free
        return disk_free

    def run_command(args: list[str]) -> str:
        commands.append(args)
        program, option = args[0], args[-1]
        if program == "ffprobe" and ffprobe:
            return f"ffprobe version {ffmpeg_version} Copyright (c) 2007-2023 the FFmpeg developers\n"
        if program == "ffmpeg" and ffmpeg:
            outputs = {
                "-version": f"ffmpeg version {ffmpeg_version} Copyright (c) 2000-2023 the FFmpeg developers\n",
                "-encoders": encoders_output(encoders),
                "-filters": filters_output(filters),
                "-hwaccels": hwaccels_output(hwaccels),
            }
            return outputs[option]
        raise FileNotFoundError(program)

    def http_get_json(url: str):
        http_calls.append(url)
        if ollama is None:
            raise ConnectionRefusedError(url)
        if url.endswith("/api/tags"):
            return {"models": ollama["tags"]}
        if url.endswith("/api/ps"):
            return {"models": ollama["ps"]}
        raise AssertionError(f"unexpected URL: {url}")

    env = DetectionEnv(
        meminfo_path=meminfo,
        cgroup_root=cgroup_root,
        cpu_affinity=cpu_affinity,
        disk_usage=disk_usage,
        run_command=run_command,
        http_get_json=http_get_json,
    )
    env.commands = commands  # type: ignore[attr-defined]
    env.http_calls = http_calls  # type: ignore[attr-defined]
    return env


def ollama_with_model(*, loaded: dict | None = None, name: str = MODEL, size: int = MODEL_SIZE) -> dict:
    """An Ollama that has the model; `loaded` is the `/api/ps` entry (e.g. `{"size_vram": 0}`)."""
    ps = [] if loaded is None else [{"name": name, "model": name, "size": size, **loaded}]
    return {"tags": [{"name": name, "model": name, "size": size}], "ps": ps}


def write_cgroup_v2(
    env: DetectionEnv, *, cpu_max=None, memory_max=None, memory_current=None, inactive_file=0
) -> None:
    root = env.cgroup_root
    (root / "cgroup.controllers").write_text("cpu memory\n")
    if cpu_max is not None:
        (root / "cpu.max").write_text(f"{cpu_max}\n")
    if memory_max is not None:
        (root / "memory.max").write_text(f"{memory_max}\n")
        (root / "memory.current").write_text(f"{memory_current or 0}\n")
        (root / "memory.stat").write_text(f"anon 1\ninactive_file {inactive_file}\nactive_file 2\n")


def write_cgroup_v1(
    env: DetectionEnv, *, cfs_quota=None, cfs_period=100_000, memory_limit=None, memory_usage=None, inactive_file=0
) -> None:
    root = env.cgroup_root
    if cfs_quota is not None:
        (root / "cpu").mkdir(exist_ok=True)
        (root / "cpu" / "cpu.cfs_quota_us").write_text(f"{cfs_quota}\n")
        (root / "cpu" / "cpu.cfs_period_us").write_text(f"{cfs_period}\n")
    if memory_limit is not None:
        (root / "memory").mkdir(exist_ok=True)
        (root / "memory" / "memory.limit_in_bytes").write_text(f"{memory_limit}\n")
        (root / "memory" / "memory.usage_in_bytes").write_text(f"{memory_usage or 0}\n")
        (root / "memory" / "memory.stat").write_text(f"cache 1\ntotal_inactive_file {inactive_file}\n")
