"""`doctor`: checks the installation without touching the media or doing heavy work.

Blocking problems (missing FFmpeg, encoders or filters, no space, unwritable workdir) make the
command exit with code 4. Ollama problems block only with `--require-ai`, the same rule as a
normal run; everything else with a documented degradation is a warning.
"""

import importlib.util
import os
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from vacation_memories.config import Settings
from vacation_memories.media_scanner import heic_available as _heic_available
from vacation_memories.runtime_profile import GIB, MIB, RuntimeProfile, format_profile

MIN_PYTHON = (3, 11)
REQUIRED_FFMPEG_ENCODERS = ("libx264", "aac")
# Single source of truth for the filters the renderer uses; the Dockerfile build check repeats it
# and a test keeps the two lists equal.
REQUIRED_FFMPEG_FILTERS = (
    "xfade",
    "zoompan",
    "fps",
    "scale",
    "boxblur",
    "overlay",
    "format",
    "setsar",
    "afade",
    "acrossfade",
    "anullsrc",
)
# Used for the HDR to SDR conversion; without them the conversion degrades (section 8).
OPTIONAL_FFMPEG_FILTERS = ("zscale", "tonemap")
REQUIRED_PYTHON_MODULES = ("numpy", "PIL", "tzdata")
MIN_FREE_BYTES = 512 * MIB

OK, WARN, FAIL = "ok", "warn", "fail"
_TAGS = {OK: " OK ", WARN: "WARN", FAIL: "FAIL"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_doctor(
    settings: Settings,
    profile: RuntimeProfile,
    *,
    python_version: tuple[int, ...] | None = None,
    image_version: str | None = None,
    module_available: Callable[[str], bool] = _module_available,
    heic_available: Callable[[], bool] = _heic_available,
) -> list[Check]:
    python_version = python_version or tuple(sys.version_info[:3])
    image_version = os.environ.get("IMAGE_VERSION") if image_version is None else image_version
    checks = [_check_python(python_version), _check_image(image_version)]
    checks += _check_ffmpeg(profile)
    checks += _check_libraries(module_available, heic_available)
    checks += _check_workdir(settings.workdir, profile)
    checks.append(_check_limits(profile))
    checks.append(_check_ollama(settings, profile))
    return checks


def has_blocking_failures(checks: Iterable[Check]) -> bool:
    return any(check.status == FAIL for check in checks)


def format_report(checks: list[Check], profile: RuntimeProfile) -> list[str]:
    lines = ["vacation-memories doctor"]
    lines += [f"[{_TAGS[check.status]}] {check.name}: {check.message}" for check in checks]
    lines.append("Runtime profile:")
    lines += [f"  {line}" for line in format_profile(profile)]
    warnings = sum(check.status == WARN for check in checks)
    if has_blocking_failures(checks):
        lines.append("Result: blocked")
    elif warnings:
        lines.append(f"Result: ok with {warnings} warning(s)")
    else:
        lines.append("Result: ok")
    return lines


def _check_python(version: tuple[int, ...]) -> Check:
    found = ".".join(map(str, version))
    if version[:2] < MIN_PYTHON:
        return Check("python", FAIL, f"Python {'.'.join(map(str, MIN_PYTHON))} or higher is required (found {found})")
    return Check("python", OK, f"Python {found}")


def _check_image(version: str | None) -> Check:
    if not version:
        return Check("image", OK, "not running in a release image (no version recorded)")
    return Check("image", OK, f"image version {version}")


def _check_ffmpeg(profile: RuntimeProfile) -> list[Check]:
    ffmpeg = profile.ffmpeg
    checks = []
    for name, version in (("ffmpeg", ffmpeg.ffmpeg_version), ("ffprobe", ffmpeg.ffprobe_version)):
        if version is None:
            checks.append(Check(name, FAIL, f"{name} not found or not working"))
        else:
            checks.append(Check(name, OK, f"version {version}"))
    if ffmpeg.ffmpeg_version is None:
        return checks

    missing_encoders = [name for name in REQUIRED_FFMPEG_ENCODERS if name not in ffmpeg.encoders]
    checks.append(
        Check("ffmpeg-encoders", FAIL, f"missing encoder(s): {', '.join(missing_encoders)}")
        if missing_encoders
        else Check("ffmpeg-encoders", OK, ", ".join(REQUIRED_FFMPEG_ENCODERS))
    )
    missing_filters = [name for name in REQUIRED_FFMPEG_FILTERS if name not in ffmpeg.filters]
    checks.append(
        Check("ffmpeg-filters", FAIL, f"missing filter(s): {', '.join(missing_filters)}")
        if missing_filters
        else Check("ffmpeg-filters", OK, f"all {len(REQUIRED_FFMPEG_FILTERS)} required filters present")
    )
    missing_optional = [name for name in OPTIONAL_FFMPEG_FILTERS if name not in ffmpeg.filters]
    checks.append(
        Check(
            "ffmpeg-optional-filters",
            WARN,
            f"missing {', '.join(missing_optional)}: the HDR to SDR conversion is degraded (specification, section 8)",
        )
        if missing_optional
        else Check("ffmpeg-optional-filters", OK, ", ".join(OPTIONAL_FFMPEG_FILTERS))
    )
    return checks


def _check_libraries(module_available: Callable[[str], bool], heic_available: Callable[[], bool]) -> list[Check]:
    missing = [name for name in REQUIRED_PYTHON_MODULES if not module_available(name)]
    libraries = (
        Check("python-libraries", FAIL, f"missing required librar{'y' if len(missing) == 1 else 'ies'}: {', '.join(missing)}")
        if missing
        else Check("python-libraries", OK, ", ".join(REQUIRED_PYTHON_MODULES))
    )
    heic = (
        Check("pillow-heif", OK, "HEIC decoder registered")
        if heic_available()
        else Check("pillow-heif", WARN, "HEIC decoder not available: HEIC files will be skipped with a warning")
    )
    return [libraries, heic]


def _writable(folder: Path) -> str | None:
    """None if a file can be created in `folder`, otherwise the reason."""
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".doctor-"):
            return None
    except OSError as error:
        return error.strerror or str(error)


def _check_workdir(workdir: Path, profile: RuntimeProfile) -> list[Check]:
    checks = []
    for name, folder in (("workdir", workdir), ("tmp", workdir / "tmp")):
        reason = _writable(folder)
        checks.append(
            Check(name, OK, f"{folder} is writable") if reason is None else Check(name, FAIL, f"{folder} is not writable: {reason}")
        )

    free = {"workdir": profile.workdir_free_bytes, "tmp": profile.tmp_free_bytes}
    known = {name: value for name, value in free.items() if value is not None}
    low = [name for name, value in known.items() if value < MIN_FREE_BYTES]
    if low:
        checks.append(Check("free-space", FAIL, f"less than {MIN_FREE_BYTES // MIB} MiB free in: {', '.join(low)}"))
    elif not known:
        checks.append(Check("free-space", WARN, "free disk space could not be read"))
    else:
        checks.append(Check("free-space", OK, ", ".join(f"{name} {value / GIB:.1f} GiB free" for name, value in known.items())))
    return checks


def _check_limits(profile: RuntimeProfile) -> Check:
    cpu = "none" if profile.cpu_cgroup_limit is None else f"{profile.cpu_cgroup_limit:g} CPU"
    ram = "none" if profile.cgroup_ram_headroom_bytes is None else f"{profile.cgroup_ram_headroom_bytes / GIB:.1f} GiB left"
    return Check(
        "limits",
        OK,
        f"usable: {profile.usable_cores} core(s), {profile.usable_ram_bytes / GIB:.1f} GiB RAM "
        f"(cgroup CPU limit: {cpu}; cgroup memory: {ram})",
    )


def _check_ollama(settings: Settings, profile: RuntimeProfile) -> Check:
    ollama = profile.ollama
    if settings.no_ai or ollama is None:
        return Check("ollama", OK, "not checked (--no-ai)")
    problem_status = FAIL if settings.require_ai else WARN
    consequence = "" if settings.require_ai else ": the AI analysis will be skipped"
    if not ollama.reachable:
        return Check("ollama", problem_status, f"not reachable at {settings.ollama_url}{consequence}")
    if not ollama.model_present:
        return Check("ollama", problem_status, f"model {ollama.model} is not installed{consequence}")
    where = f", loaded and running on {ollama.placement.upper()}" if ollama.model_loaded else ""
    return Check("ollama", OK, f"model {ollama.model} present{where}")
