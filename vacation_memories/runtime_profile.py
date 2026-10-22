"""Runtime profile: what the machine can do, detected at startup, and the values derived from it.

Nothing about the hardware is hard-wired. Detection uses only the standard library and sits behind
`DetectionEnv`, so tests replace `/proc/meminfo`, the cgroup files, the number of cores, the output of
FFmpeg and the answers of Ollama. A failed detection gives conservative values and a warning, never
a crash. The profile changes the speed and the amount of work, never the selection criteria, so none
of its values belongs in a cache key.
"""

import json
import logging
import math
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vacation_memories.atomic import atomic_write_json
from vacation_memories.config import Settings, ollama_url_is_local

logger = logging.getLogger(__name__)

MIB = 1024**2
GIB = 1024**3
CONSERVATIVE_CORES = 1
CONSERVATIVE_RAM_BYTES = 2 * GIB
COMMAND_TIMEOUT_SECONDS = 30
OLLAMA_TIMEOUT_SECONDS = 3
HW_ENCODER_SUFFIXES = ("_vaapi", "_qsv", "_nvenc", "_amf", "_videotoolbox", "_v4l2m2m")

# RAM kept for the main process, and RAM assumed per parallel job.
RESERVED_RAM_MIB = 1024
RAM_PER_JOB_MIB = 1024
# Upper bounds of the values that shrink on small machines.
MAX_BLOCK_SIZE = 40
MAX_CACHE_MEMORY_MB = 1024
RAM_MIB_PER_BLOCK_CLIP = 100
RAM_FRACTION_FOR_CACHE = 8
UNLIMITED_CGROUP_BYTES = 2**60


def _run_command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=True,
    )
    return result.stdout


def _http_get_json(url: str) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
        return json.load(response)


@dataclass
class DetectionEnv:
    """Everything detection reads from the outside world; the defaults are the real system."""

    meminfo_path: Path = Path("/proc/meminfo")
    cgroup_root: Path = Path("/sys/fs/cgroup")
    cpu_affinity: Callable[[], int] = lambda: len(os.sched_getaffinity(0))
    disk_usage: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free
    run_command: Callable[[list[str]], str] = _run_command
    http_get_json: Callable[[str], object] = _http_get_json


@dataclass(frozen=True)
class FFmpegInfo:
    ffmpeg_version: str | None
    ffprobe_version: str | None
    encoders: frozenset[str]
    filters: frozenset[str]
    hwaccels: tuple[str, ...]
    hw_encoders: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "ffmpeg_version": self.ffmpeg_version,
            "ffprobe_version": self.ffprobe_version,
            "has_libx264": "libx264" in self.encoders,
            "has_aac": "aac" in self.encoders,
            "hwaccels": list(self.hwaccels),
            "hw_encoders": list(self.hw_encoders),
        }


@dataclass(frozen=True)
class OllamaInfo:
    """`placement` is where the model runs: gpu, cpu, mixed, or unknown (not loaded)."""

    reachable: bool
    model: str
    models: tuple[str, ...] = ()
    model_present: bool = False
    model_size_bytes: int | None = None
    model_loaded: bool = False
    placement: str = "unknown"
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "model": self.model,
            "models": list(self.models),
            "model_present": self.model_present,
            "model_size_bytes": self.model_size_bytes,
            "model_loaded": self.model_loaded,
            "placement": self.placement,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuntimeProfile:
    usable_cores: int
    cpu_affinity: int | None
    cpu_cgroup_limit: float | None
    usable_ram_bytes: int
    host_ram_available_bytes: int | None
    cgroup_ram_headroom_bytes: int | None
    model_reserved_bytes: int
    workdir_free_bytes: int | None
    tmp_free_bytes: int | None
    ffmpeg: FFmpegInfo
    ollama: OllamaInfo | None
    jobs: int
    threads_per_job: int
    block_size: int
    cache_memory_mb: int
    hwaccel: str  # what the renderer may use: "none", "auto" or an accelerator FFmpeg has
    hwaccel_requested: str
    overrides: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cpu": {
                "usable_cores": self.usable_cores,
                "affinity": self.cpu_affinity,
                "cgroup_limit": self.cpu_cgroup_limit,
            },
            "memory": {
                "usable_bytes": self.usable_ram_bytes,
                "host_available_bytes": self.host_ram_available_bytes,
                "cgroup_headroom_bytes": self.cgroup_ram_headroom_bytes,
                "model_reserved_bytes": self.model_reserved_bytes,
            },
            "disk": {"workdir_free_bytes": self.workdir_free_bytes, "tmp_free_bytes": self.tmp_free_bytes},
            "ffmpeg": self.ffmpeg.to_dict(),
            "ollama": None if self.ollama is None else self.ollama.to_dict(),
            "derived": {
                "jobs": self.jobs,
                "threads_per_job": self.threads_per_job,
                "block_size": self.block_size,
                "cache_memory_mb": self.cache_memory_mb,
                "hwaccel": self.hwaccel,
                "hwaccel_requested": self.hwaccel_requested,
            },
            "overrides": self.overrides,
            "warnings": self.warnings,
        }


def detect_profile(settings: Settings, env: DetectionEnv | None = None) -> RuntimeProfile:
    env = env or DetectionEnv()
    warnings: list[str] = []

    def warn(message: str) -> None:
        logger.warning(message)
        warnings.append(message)

    affinity = _detect_affinity(env, warn)
    cgroup_cpu = _cgroup_cpu_limit(env, warn)
    usable_cores = _usable_cores(affinity, cgroup_cpu)

    ffmpeg = detect_ffmpeg(env, warn)
    ai_active = not settings.no_ai
    ollama = detect_ollama(env, settings.ollama_url, settings.model, warn) if ai_active else None

    host_ram = _host_ram_available(env, warn)
    reserved = _model_reservation(ollama, settings.ollama_url)
    headroom = _cgroup_ram_headroom(env, warn)
    usable_ram = max(0, (host_ram if host_ram is not None else CONSERVATIVE_RAM_BYTES) - reserved)
    if headroom is not None:
        usable_ram = min(usable_ram, headroom)

    jobs, threads, block_size, cache_mb, overrides = _derive(settings, usable_cores, usable_ram, warn)
    return RuntimeProfile(
        usable_cores=usable_cores,
        cpu_affinity=affinity,
        cpu_cgroup_limit=cgroup_cpu,
        usable_ram_bytes=usable_ram,
        host_ram_available_bytes=host_ram,
        cgroup_ram_headroom_bytes=headroom,
        model_reserved_bytes=reserved,
        workdir_free_bytes=_free_space(env, settings.workdir, warn),
        tmp_free_bytes=_free_space(env, settings.workdir / "tmp", warn),
        ffmpeg=ffmpeg,
        ollama=ollama,
        jobs=jobs,
        threads_per_job=threads,
        block_size=block_size,
        cache_memory_mb=cache_mb,
        hwaccel=_resolve_hwaccel(settings.hwaccel, ffmpeg, warn),
        hwaccel_requested=settings.hwaccel,
        overrides=overrides,
        warnings=warnings,
    )


def write_runtime_profile(profile: RuntimeProfile, path: Path) -> None:
    atomic_write_json(path, profile.to_dict())


def format_profile(profile: RuntimeProfile) -> list[str]:
    """Human-readable lines for the startup log and the `doctor` report."""

    def gib(value: int | None) -> str:
        return "unknown" if value is None else f"{value / GIB:.1f} GiB"

    cgroup_cpu = "none" if profile.cpu_cgroup_limit is None else f"{profile.cpu_cgroup_limit:g}"
    cgroup_ram = "none" if profile.cgroup_ram_headroom_bytes is None else gib(profile.cgroup_ram_headroom_bytes)
    lines = [
        f"CPU: {profile.usable_cores} core(s) usable (affinity {profile.cpu_affinity}, cgroup limit {cgroup_cpu})",
        f"RAM: {gib(profile.usable_ram_bytes)} usable (host available {gib(profile.host_ram_available_bytes)}, "
        f"cgroup headroom {cgroup_ram}, reserved for the model {gib(profile.model_reserved_bytes)})",
        f"Disk: workdir {gib(profile.workdir_free_bytes)} free, tmp {gib(profile.tmp_free_bytes)} free",
        f"FFmpeg: {profile.ffmpeg.ffmpeg_version or 'not found'}, "
        f"accelerations: {', '.join(profile.ffmpeg.hwaccels) or 'none'}",
    ]
    if profile.ollama is not None:
        ollama = profile.ollama
        state = "unreachable" if not ollama.reachable else f"model {'present' if ollama.model_present else 'missing'}"
        lines.append(f"Ollama: {state}, model runs on: {ollama.placement}")
    requested = "" if profile.hwaccel_requested == profile.hwaccel else f" (requested {profile.hwaccel_requested})"
    lines.append(
        f"Derived: jobs {profile.jobs} x {profile.threads_per_job} thread(s), block size {profile.block_size}, "
        f"memory cache {profile.cache_memory_mb} MB, hwaccel {profile.hwaccel}{requested}"
    )
    if profile.overrides:
        lines.append("Overridden by configuration: " + ", ".join(f"{k}={v}" for k, v in profile.overrides.items()))
    return lines


# --- CPU -------------------------------------------------------------------------------------


def _detect_affinity(env: DetectionEnv, warn: Callable[[str], None]) -> int | None:
    try:
        return env.cpu_affinity()
    except Exception as error:
        warn(f"cannot detect the CPU cores, assuming {CONSERVATIVE_CORES}: {error}")
        return None


def _usable_cores(affinity: int | None, cgroup_limit: float | None) -> int:
    cores = CONSERVATIVE_CORES if affinity is None else affinity
    if cgroup_limit is not None:
        cores = min(cores, math.floor(cgroup_limit))
    return max(1, cores)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _cgroup_cpu_limit(env: DetectionEnv, warn: Callable[[str], None]) -> float | None:
    """CPUs granted by the cgroup quota (v2 `cpu.max`, v1 `cpu.cfs_quota_us` / `cpu.cfs_period_us`)."""
    root = env.cgroup_root
    if (text := _read(root / "cpu.max")) is not None:
        try:
            quota, period = text.split()[:2]
            return None if quota == "max" else int(quota) / int(period)
        except (ValueError, ZeroDivisionError):
            warn(f"cannot parse the cgroup file cpu.max ({text!r}), ignoring the CPU quota")
            return None
    for folder in (root / "cpu", root / "cpu,cpuacct"):
        quota, period = _read(folder / "cpu.cfs_quota_us"), _read(folder / "cpu.cfs_period_us")
        if quota is None or period is None:
            continue
        try:
            return None if int(quota) <= 0 else int(quota) / int(period)
        except (ValueError, ZeroDivisionError):
            warn("cannot parse the cgroup CFS quota files, ignoring the CPU quota")
    return None


# --- Memory ----------------------------------------------------------------------------------


def _host_ram_available(env: DetectionEnv, warn: Callable[[str], None]) -> int | None:
    try:
        for line in env.meminfo_path.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
        raise ValueError("MemAvailable not found")
    except (OSError, ValueError, IndexError) as error:
        warn(f"cannot read the available memory, assuming {CONSERVATIVE_RAM_BYTES // GIB} GiB: {error}")
        return None


def _stat_value(text: str | None, key: str) -> int:
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if name == key:
            return int(value)
    return 0


def _cgroup_ram_headroom(env: DetectionEnv, warn: Callable[[str], None]) -> int | None:
    """Room left under the cgroup limit, computed on the working set.

    `memory.current` alone includes the page cache, which fills the limit after reading many media
    files even though the kernel can reclaim it, so the inactive file pages are taken out.
    """
    root = env.cgroup_root
    try:
        if (limit := _read(root / "memory.max")) is not None:  # cgroup v2
            if limit == "max":
                return None
            used = int(_read(root / "memory.current") or 0) - _stat_value(_read(root / "memory.stat"), "inactive_file")
            return max(0, int(limit) - used)
        folder = root / "memory"  # cgroup v1
        if (limit := _read(folder / "memory.limit_in_bytes")) is not None:
            if int(limit) >= UNLIMITED_CGROUP_BYTES:
                return None
            used = int(_read(folder / "memory.usage_in_bytes") or 0) - _stat_value(
                _read(folder / "memory.stat"), "total_inactive_file"
            )
            return max(0, int(limit) - used)
    except ValueError as error:
        warn(f"cannot parse the cgroup memory files, ignoring the memory limit: {error}")
    return None


def _model_reservation(ollama: OllamaInfo | None, url: str) -> int:
    """A model not loaded yet will take this much host RAM when Ollama runs on this machine
    (also when it runs on the integrated GPU, which uses system RAM). A remote Ollama uses
    its own host's memory."""
    if ollama is None or not ollama.model_present or ollama.model_loaded or not ollama_url_is_local(url):
        return 0
    return ollama.model_size_bytes or 0


def _free_space(env: DetectionEnv, path: Path, warn: Callable[[str], None]) -> int | None:
    try:
        return env.disk_usage(path)
    except OSError as error:
        warn(f"cannot read the free disk space of {path}: {error}")
        return None


# --- FFmpeg ----------------------------------------------------------------------------------


def detect_ffmpeg(env: DetectionEnv, warn: Callable[[str], None]) -> FFmpegInfo:
    ffmpeg_version = _tool_version(env, ["ffmpeg", "-hide_banner", "-nostdin"], "ffmpeg", warn)
    ffprobe_version = _tool_version(env, ["ffprobe", "-hide_banner"], "ffprobe", warn)
    encoders: frozenset[str] = frozenset()
    filters: frozenset[str] = frozenset()
    hwaccels: tuple[str, ...] = ()
    if ffmpeg_version is not None:
        encoders = frozenset(_listing(env, "-encoders", warn))
        filters = frozenset(_listing(env, "-filters", warn))
        hwaccels = tuple(_hwaccels(env, warn))
    hw_encoders = tuple(sorted(name for name in encoders if name.endswith(HW_ENCODER_SUFFIXES)))
    return FFmpegInfo(ffmpeg_version, ffprobe_version, encoders, filters, hwaccels, hw_encoders)


def _tool_version(env: DetectionEnv, command: list[str], name: str, warn: Callable[[str], None]) -> str | None:
    try:
        first_line = env.run_command([*command, "-version"]).splitlines()[0]
    except Exception as error:
        warn(f"{name} is not usable: {error}")
        return None
    words = first_line.split()
    return words[words.index("version") + 1] if "version" in words[:-1] else "unknown"


def _listing(env: DetectionEnv, option: str, warn: Callable[[str], None]) -> list[str]:
    """Names from `ffmpeg -encoders` or `-filters`: the second column after the `------` line."""
    try:
        lines = env.run_command(["ffmpeg", "-hide_banner", "-nostdin", option]).splitlines()
    except Exception as error:
        warn(f"cannot list the FFmpeg {option[1:]}: {error}")
        return []
    start = next((index for index, line in enumerate(lines) if line.strip().startswith("---")), -1)
    return [tokens[1] for line in lines[start + 1 :] if len(tokens := line.split()) >= 2]


def _hwaccels(env: DetectionEnv, warn: Callable[[str], None]) -> list[str]:
    try:
        lines = env.run_command(["ffmpeg", "-hide_banner", "-nostdin", "-hwaccels"]).splitlines()
    except Exception as error:
        warn(f"cannot list the FFmpeg accelerations: {error}")
        return []
    return [line.strip() for line in lines[1:] if line.strip()]


# --- Ollama ----------------------------------------------------------------------------------


def _model_key(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def _entry_name(entry: dict) -> str:
    return entry.get("name") or entry.get("model") or ""


def _models(payload: object) -> list[dict]:
    """The `models` entries of an Ollama answer. A null or malformed list (Go servers emit `null`
    for an empty slice) counts as empty; entries that are not objects are skipped."""
    entries = payload.get("models") if isinstance(payload, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def detect_ollama(env: DetectionEnv, url: str, model: str, warn: Callable[[str], None]) -> OllamaInfo:
    try:
        tags = _models(env.http_get_json(f"{url}/api/tags"))
    except Exception as error:
        warn(f"Ollama is not reachable at {url}: {error}")
        return OllamaInfo(reachable=False, model=model, error=str(error))

    wanted = _model_key(model)
    installed = next((entry for entry in tags if _model_key(_entry_name(entry)) == wanted), None)
    try:
        running = _models(env.http_get_json(f"{url}/api/ps"))
    except Exception as error:
        warn(f"cannot read the Ollama state of the loaded models: {error}")
        running = []
    loaded = next((entry for entry in running if _model_key(_entry_name(entry)) == wanted), None)

    return OllamaInfo(
        reachable=True,
        model=model,
        models=tuple(_entry_name(entry) for entry in tags),
        model_present=installed is not None,
        model_size_bytes=None if installed is None else installed.get("size"),
        model_loaded=loaded is not None,
        placement="unknown" if loaded is None else _placement(loaded),
    )


def loaded_model_placement(env: DetectionEnv, url: str, model: str) -> str:
    """Where the model runs now (gpu, cpu, mixed), or "unknown" if it is not loaded or Ollama does not answer."""
    try:
        running = _models(env.http_get_json(f"{url}/api/ps"))
    except Exception as error:
        logger.warning("cannot read the Ollama state of the loaded models: %s", error)
        return "unknown"
    wanted = _model_key(model)
    loaded = next((entry for entry in running if _model_key(_entry_name(entry)) == wanted), None)
    return "unknown" if loaded is None else _placement(loaded)


def record_model_placement(path: Path, placement: str) -> bool:
    """Write where the model runs into an existing `runtime_profile.json`; False if there was nothing to update."""
    if placement == "unknown":
        return False
    try:
        data = json.loads(path.read_text())
        ollama = data["ollama"]
        ollama["placement"] = placement
        ollama["model_loaded"] = True
        atomic_write_json(path, data)
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("cannot record where the model runs in %s", path)
        return False
    return True


def _placement(loaded: dict) -> str:
    """Where a loaded model runs, from the ratio between `size_vram` and `size`, as `ollama ps` does."""
    size, in_vram = loaded.get("size") or 0, loaded.get("size_vram") or 0
    if size <= 0:
        return "unknown"
    if in_vram <= 0:
        return "cpu"
    return "gpu" if in_vram >= size else "mixed"


# --- Derived parameters ----------------------------------------------------------------------


def _resolve_hwaccel(requested: str, ffmpeg: FFmpegInfo, warn: Callable[[str], None]) -> str:
    """Keep the request only if FFmpeg has a matching hardware encoder; otherwise use software.

    `auto` stays `auto`: the test encode that decides whether the accelerator really works belongs
    to the encoder choice of the renderer.
    """
    if requested == "none":
        return "none"
    if requested == "auto":
        if ffmpeg.hw_encoders:
            return "auto"
        warn("hwaccel auto: no hardware encoder available in FFmpeg, using software encoding")
        return "none"
    if any(name.endswith(f"_{requested}") for name in ffmpeg.hw_encoders):
        return requested
    warn(f"hwaccel {requested}: FFmpeg has no {requested} encoder, using software encoding")
    return "none"


def _derive(
    settings: Settings, cores: int, usable_ram: int, warn: Callable[[str], None]
) -> tuple[int, int, int, int, dict[str, object]]:
    ram_mib = usable_ram // MIB
    overrides: dict[str, object] = {}
    for key in ("jobs", "threads_per_job", "block_size", "cache_memory_mb"):
        value = getattr(settings, key)
        if value is not None and value != "auto":
            overrides[key] = value

    jobs = overrides.get("jobs") or max(1, min(cores, (ram_mib - RESERVED_RAM_MIB) // RAM_PER_JOB_MIB))
    threads = overrides.get("threads_per_job") or max(1, cores // jobs)
    if jobs * threads > cores:
        warn(f"jobs x threads ({jobs} x {threads}) exceeds the {cores} usable cores: expect oversubscription")
    block_size = overrides.get("block_size") or max(1, min(MAX_BLOCK_SIZE, ram_mib // RAM_MIB_PER_BLOCK_CLIP))
    cache_mb = overrides.get("cache_memory_mb") or max(1, min(MAX_CACHE_MEMORY_MB, ram_mib // RAM_FRACTION_FOR_CACHE))
    return jobs, threads, block_size, cache_mb, overrides
